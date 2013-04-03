#!/usr/bin/env python

#----------------------------------------------------------------------
# Copyright (c) 2013 Raytheon BBN Technologies
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and/or hardware specification (the "Work") to
# deal in the Work without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Work, and to permit persons to whom the Work
# is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Work.
#
# THE WORK IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE WORK OR THE USE OR OTHER DEALINGS
# IN THE WORK.
#----------------------------------------------------------------------
'''Main stitching workhorse. Handles calling the stitching service, orchestrating
parsing RSpecs and creating objects. See doStitching().'''
import copy
import datetime
import logging
import os
import string
import time

import omni
from omnilib.util import OmniError, naiveUTC
import omnilib.util.credparsing as credutils
from omnilib.util.files import readFile
import omnilib.util.handler_utils as handler_utils

import omnilib.stitch as stitch
from omnilib.stitch.ManifestRSpecCombiner import combineManifestRSpecs
from omnilib.stitch.objects import Aggregate, Link
from omnilib.stitch import RSpecParser
import omnilib.stitch.scs as scs
from omnilib.stitch.workflow import WorkflowParser
from omnilib.stitch.utils import StitchingError, StitchingCircuitFailedError, stripBlankLines

from geni.util import rspec_schema
from geni.util.rspec_util import is_rspec_string, is_rspec_of_type, rspeclint_exists, validate_rspec, getPrettyRSpec

DCN_AM_TYPE = 'dcn' # geni_am_type value from AMs that use the DCN codebae

# Max # of times to call the stitching service
MAX_SCS_CALLS = 5

# The main stitching class. Holds all the state about our attempt at doing stitching.
class StitchingHandler(object):
    '''Workhorse class to do stitching. See doStitching().'''

    def __init__(self, opts, config, logger):
        self.logger = logger
        config['logger'] = logger
        self.omni_config = config['omni']
        self.config = config
        self.parsedSCSRSpec = None
        self.ams_to_process = []
        self.opts = opts # command line options as parsed
        self.framework = omni.load_framework(self.config, self.opts)
        # FIXME: How many times is right to go back to the SCS
        self.maxSCSCalls = MAX_SCS_CALLS

        # Remember we got the extra info for this AM
        self.amURNsAddedInfo = []

    def doStitching(self, args):
        '''Main stitching function.'''
        # Get request RSpec
        request = None
        command = None
        self.slicename = None
        if len(args) > 0:
            command = args[0]
        if not command or command.strip().lower() not in ('createsliver', 'allocate'):
            # Stitcher only handles createsliver or allocate
            if self.opts.fakeModeDir:
                msg = "In fake mode. Otherwise would call Omni with args %r" % args
                self.logger.info(msg)
                return (msg, None)
            else:
                self.logger.debug("Passing call to Omni")
                return omni.call(args, self.opts)

        if len(args) > 1:
            self.slicename = args[1]
        if len(args) > 2:
            request = args[2]

        if len(args) > 3:
            self.logger.warn("Arguments %s ignored", args[3:])
        #self.logger.debug("Command=%s, slice=%s, rspec=%s", command, self.slicename, request)

        # Parse the RSpec
        requestString = ""
        self.rspecParser = RSpecParser.RSpecParser(self.logger)
        self.parsedUserRequest = None
        if request:
            try:
                # read the rspec into a string, and add it to the rspecs dict
                requestString = readFile(request)
            except Exception, exc:
                msg = 'Unable to read rspec file %s: %s' % (request, str(exc))
                raise OmniError(msg)

            #    # Test if the rspec is really json containing an RSpec, and pull out the right thing
            #    requestString = amhandler.self._maybeGetRSpecFromStruct(requestString)

            # confirmGoodRequest
            self.confirmGoodRSpec(requestString)
            self.logger.debug("Valid GENI v3 request RSpec")
            
            # parseRequest
            self.parsedUserRequest = self.rspecParser.parse(requestString)
            
        # If this is not a real stitching thing, just let Omni handle this.
        if not self.mustCallSCS(self.parsedUserRequest):
            # Warning: If this is createsliver and you specified multiple aggregates,
            # then omni only contacts 1 aggregate. That is likely not what you wanted.
            return omni.call(args, self.opts)

        # FIXME: Confirm request is not asking for any loops
        self.confirmSafeRequest()

        # Remove any -a arguments from the opts so that when we later call omni
        # the right thing happens
        self.opts.aggregate = []

        # FIXME: Maybe use threading to parallelize confirmSliceOK and the 1st SCS call?

        # Ensure the slice is valid before all those Omni calls use it
        (sliceurn, sliceexp) = self.confirmSliceOK()

        # Ensure expires attribute set for DCN AMs
        self.addExpiresAttribute(self.parsedUserRequest.dom, sliceexp)
    
        self.scsService = scs.Service(self.opts.scsURL)
        self.scsCalls = 0

        # Call SCS and then do reservations at AMs, deleting or retrying SCS as needed
        try:
            # Passing in the request as a DOM. OK?
            lastAM = self.mainStitchingLoop(sliceurn, self.parsedUserRequest.dom)

            # Construct a unified manifest
            # include AMs, URLs, API versions
            combinedManifest = self.combineManifests(self.ams_to_process, lastAM)

            # FIXME: Handle errors. Maybe make return use code/value/output struct
            # If error and have an expanded rquest from SCS, include that in output.
            #   Or if particular AM had errors, ID the AMs and errors

            # FIXME: This prepends a header on an RSpec that might already have a header
            # -- maybe replace any existing header
            # FIXME: Without the -o option this is really verbose! Maybe set -o?
            retVal, filename = handler_utils._writeRSpec(self.opts, self.logger, combinedManifest, self.slicename, 'stitching-combined', '', None)
            if filename:
                self.logger.info("Saved combined reservation RSpec at %d AMs to file %s", len(self.ams_to_process), filename)

        except StitchingError, se:
            # FIXME: Return anything different for stitching error?
            # Do we want to return a geni triple struct?
            raise
        finally:
            self.cleanup()

            if self.opts.debug:
                self.dump_objects(self.parsedSCSRSpec, self.ams_to_process)

        # Construct return
        amcnt = len(self.ams_to_process)
        scs_added_amcnt = 0
        pathcnt = 0
        if self.parsedSCSRSpec and self.parsedSCSRSpec.stitching:
            pathcnt = len(self.parsedSCSRSpec.stitching.paths)
        for am in self.ams_to_process:
            if not am.userRequested:
                scs_added_amcnt = scs_added_amcnt + 1
        retMsg = "Stitching success: Reserved resources in slice %s at %d Aggregates (including %d intermediate aggregate(s) not in the original request), creating %d link(s)." % (self.slicename, amcnt, scs_added_amcnt, pathcnt)
 
        # FIXME: What do we want to return?
# Make it something like createsliver / allocate, with the code/value/output triple plus a string
# On success
#  Request from SCS that worked? Merged request as I modified?
#  Merged manifest
#  List of AMs, the URLs, and their API versions?
#  Some indication of the slivers and their status and expiration at each AM?
#    In particular, which AMs need a provision and poa geni_start
#  ?? Stuff parsed from manifest?? EG some representation of each path with node list/count at each AM and VLAN tag for each link?, maybe list of the AMs added by the SCS?
#On error
#  Error code / message (standard GENI triple)
#  If the error was after SCS, include the expanded request from the SCS
#  If particular AMs had errors, ID those AMs and the errors
        return (retMsg, combinedManifest)

    def cleanup(self):
        '''Remove temporary files if not in debug mode or if output not specified'''
        if self.opts.debug or self.opts.output: 
            return
        
        if os.path.exists(Aggregate.FAKEMODESCSFILENAME):
            os.unlink(Aggregate.FAKEMODESCSFILENAME)

        if not self.ams_to_process:
            return

        for am in self.ams_to_process:
            if am.rspecfileName:
                if os.path.exists(am.rspecfileName):
                    os.unlink(am.rspecfileName)

    def mainStitchingLoop(self, sliceurn, requestDOM, existingAggs=None):
        # existingAggs are Aggregate objects
        self.scsCalls = self.scsCalls + 1
        if self.scsCalls > 1:
            self.logger.info("Calling SCS for the %dth and last time...", self.scsCalls)

        scsResponse = self.callSCS(sliceurn, requestDOM, existingAggs)

        # Parse SCS Response, constructing objects and dependencies, validating return
        self.parsedSCSRSpec, workflow_parser = self.parseSCSResponse(scsResponse)
        scsResponse = None # Just to note we are done with this here (keep no state)

        if existingAggs:
            # Copy existingAggs.hops.vlans_unavailable to workflow_parser.aggs.hops.vlans_unavailable? Other state?
            self.saveAggregateState(existingAggs, workflow_parser.aggs)
            existingAggs = None # Now done

        # FIXME: if notScript, print AM dependency tree?

        # Ensure we are processing all the workflow aggs plus any aggs in the RSpec not in
        # the workflow
        self.ams_to_process = copy.copy(workflow_parser.aggs)
        for amURN in self.parsedSCSRSpec.amURNs:
            found = False
            for agg in self.ams_to_process:
                if agg.urn == amURN:
                    found = True
                    break
            if found:
                continue
            else:
                am = Aggregate.find(amURN)
                if not am.url:
                    self.logger.error("RSpec requires AM %s which is not in workflow and URL is unknown!", amURN)
                else:
                    self.ams_to_process.add(am)

        # The launcher handles calling the aggregates to do their allocation
        launcher = stitch.Launcher(self.opts, self.slicename, self.ams_to_process)
        try:
            # Spin up the main loop
            lastAM = launcher.launch(self.parsedSCSRSpec, self.scsCalls)
# for testing calling the SCS only many times
#            raise StitchingCircuitFailedError("testing")
        except StitchingCircuitFailedError, se:
            if self.scsCalls == self.maxSCSCalls:
                self.logger.warn("Stitching max circuit failures reached - will delete and exit.")
                self.deleteAllReservations(launcher)
                raise StitchingError("Stitching reservation failed %d times. Last error: %s" % (self.scsCalls, se))
            self.logger.warn("Stitching failed but will retry: %s", se)
            success = self.deleteAllReservations(launcher)
            if not success:
                raise StitchingError("Stitching failed. Would retry but delete had errors. Last Stitching error: %s" % se)

            # Flush the cache of aggregates. Loses all state. Avoids
            # double adding hops to aggregates, etc. But we lose the vlans_unavailable. And ?
            aggs = copy.copy(self.ams_to_process)
            self.ams_to_process = None # Clear local memory of AMs to avoid issues
            Aggregate.clearCache()

            # Let AMs recover. Is this long enough?
            self.logger.info("Pausing for %d seconds for Aggregates to free up resources...\n\n", Aggregate.PAUSE_FOR_AM_TO_FREE_RESOURCES_SECS)
            time.sleep(Aggregate.PAUSE_FOR_AM_TO_FREE_RESOURCES_SECS)

            # construct new SCS args
            # redo SCS call et al
            # FIXME: aggs.hops have loose tag: mark the hops in the request as explicitly loose
            # FIXME: Here we pass in the request to give to the SCS. I'd like this
            # to be modified (different VLAN range? Some hops marked loose?) in future
            lastAM = self.mainStitchingLoop(sliceurn, requestDOM, aggs)
        except StitchingError, se:
            self.logger.warn("Stitching failed with an error: %s", se)
            self.deleteAllReservations(launcher)
            raise se
        return lastAM

    def deleteAllReservations(self, launcher):
        '''On error exit, ensure all outstanding reservations are deleted.'''
        ret = True
        for am in launcher.aggs:
            if am.manifestDom:
                self.logger.warn("Had reservation at %s", am.url)
                try:
                    am.deleteReservation(self.opts, self.slicename)
                    self.logger.warn("Deleted reservation at %s", am.url)
                except StitchingError, se2:
                    self.logger.warn("Failed to delete reservation at %s: %s", am.url, se2)
                    ret = False
        return ret

    def confirmGoodRSpec(self, requestString, rspecType=rspec_schema.REQUEST, doRSpecLint=True):
        '''Ensure an rspec is valid'''
        typeStr = 'Request'
        if rspecType == rspec_schema.MANIFEST:
            typeStr = 'Manifest'
        # Confirm the string is a request rspec, valid
        if requestString is None or str(requestString).strip() == '':
            raise OmniError("Empty %s rspec" % typeStr)
        if not is_rspec_string(requestString, None, None, logger=self.logger):
            raise OmniError("%s RSpec file did not contain an RSpec" % typeStr)
        if not is_rspec_of_type(requestString, rspecType):
            raise OmniError("%s RSpec file did not contain a %s RSpec (wrong type or schema)" % (typeStr, typeStr))

        # Run rspeclint
        if doRSpecLint:
            try:
                rspeclint_exists()
            except:
                self.logger.debug("No rspeclint found")
                return
            # FIXME: Make this support GENIv4+?
            schema = rspec_schema.GENI_3_REQ_SCHEMA
            if rspecType == rspec_schema.MANIFEST:
                schema = rspec_schema.GENI_3_MAN_SCHEMA
            if not validate_rspec(requestString, rspec_schema.GENI_3_NAMESPACE, schema):
                raise OmniError("%s RSpec does not validate against its schemas" % typeStr)

    def confirmSliceOK(self):
        '''Ensure the given slice name corresponds to a current valid slice,
        and return the Slice URN and expiration datetime.'''

        # Get slice URN from name
        try:
            sliceurn = self.framework.slice_name_to_urn(self.slicename)
        except Exception, e:
            self.logger.error("Could not determine slice URN from name %s: %s", self.slicename, e)
            raise StitchingError(e)

        if self.opts.fakeModeDir:
            self.logger.info("Fake mode: not checking slice credential")
            return (sliceurn, naiveUTC(datetime.datetime.max))

        # Get slice cred
        (slicecred, message) = handler_utils._get_slice_cred(self, sliceurn)
        if not slicecred:
            # FIXME: Maybe if the slice doesn't exist, create it?
            # omniargs = ["createslice", self.slicename]
            # try:
            #     (slicename, message) = omni.call(omniargs, self.opts)
            # except:
            #     pass
            raise StitchingError("Could not get a slice credential for slice %s: %s" % (sliceurn, message))

        # Ensure slice not expired
        sliceexp = credutils.get_cred_exp(self.logger, slicecred)
        sliceexp = naiveUTC(sliceexp)
        now = datetime.datetime.utcnow()
        if sliceexp <= now:
            # FIXME: Maybe if the slice doesn't exist, create it?
            # omniargs = ["createslice", self.slicename]
            # try:
            #     (slicename, message) = omni.call(omniargs, self.opts)
            # except:
            #     pass
            raise StitchingError("Slice %s expired at %s" % (sliceurn, sliceexp))

        # return the slice urn, slice expiration (datetime)
        return (sliceurn, sliceexp)

    def mustCallSCS(self, requestRSpecObject):
        '''Does this request actually require stitching?
        Check: >=1 link in main body with >= 2 diff component_manager names and no shared_vlan extension'''
        if requestRSpecObject:
            for link in requestRSpecObject.links:
                if len(link.aggregates) > 1 and not link.hasSharedVlan:
                    return True
        return False

    def callSCS(self, sliceurn, requestDOM, existingAggs):
        '''Construct SCS args, call the SCS service'''

        requestString, scsOptions = self.constructSCSArgs(requestDOM, existingAggs)
        existingAggs = None # Clear to note we are done
        self.scsService.result = None # Avoid any unexpected issues

#        self.logger.debug("Calling SCS with options %s", scsOptions)
        try:
            scsResponse = self.scsService.ComputePath(sliceurn, requestString, scsOptions)
        except StitchingError as e:
            self.logger.debug("Error from slice computation service: %s", e)
            raise 
        except Exception as e:
            self.logger.error("Exception from slice computation service: %s", e)
            raise StitchingError("SCS gave error: %s" % e)

        self.logger.debug("SCS successfully returned.");

        if self.opts.debug:
            self.logger.debug("Writing SCS result JSON to scs-result.json")
            with open ("scs-result.json", 'w') as file:
                file.write(stripBlankLines(str(self.scsService.result)))

        self.scsService.result = None # Clear memory/state
        return scsResponse

    def constructSCSArgs(self, requestDOM, existingAggs=None):
        '''Build and return the string rspec request and options arguments'''
        # return requestString and options

        options = {}
        # options is a struct
        # To exclude a hop, add a geni_routing_profile struct
        # This in turn should have a struct per path whose name is the path name
        # Each shuld have a hop_exclusion_list array, containing the names of hops
        # If you append '=<VLANRange>' to the hop URN, that means to exlude
        # that set of VLANs from consideration on that hop, but don't entirely exclude
        # the hop.

#        exclude = "urn:publicid:IDN+instageni.gpolab.bbn.com+interface+procurve2:5.24=3747-3748"
#        path = "link-pg-utah1-ig-gpo1"
#        exclude = "urn:publicid:IDN+ion.internet2.edu+interface+rtr.atla:ge-7/1/6:protogeni"
#        excludes = []
#        excludes.append(exclude)
#        exclude = "urn:publicid:IDN+ion.internet2.edu+interface+rtr.hous:ge-9/1/4:protogeni"
#        excludes.append(exclude)
#        exclude = "urn:publicid:IDN+ion.internet2.edu+interface+rtr.losa:ge-7/1/3:protogeni"
#        excludes.append(exclude)
#        exclude = "urn:publicid:IDN+ion.internet2.edu+interface+rtr.salt:ge-7/1/2:*"
##        excludes.append(exclude)
#        exclude = "urn:publicid:IDN+ion.internet2.edu+interface+rtr.wash:ge-7/1/3:protogeni"
#        excludes.append(exclude)
#        profile = {}
#        pathStruct = {}
#        pathStruct["hop_exclusion_list"]=excludes
#        profile[path] = pathStruct
#        options["geni_routing_profile"]=profile

        profile = {}
        if existingAggs and len(existingAggs) > 0:
            for agg in existingAggs:
                for hop in agg.hops:
                    if hop.excludeFromSCS or (hop.vlans_unavailable and len(hop.vlans_unavailable) > 0):
                        # get path and ensure a pathStruct object
                        path = hop._path.id
                        if profile.has_key(path):
                            pathStruct = profile[path]
                        else:
                            pathStruct = {}

                        # Get hop_exclusion_list
                        if pathStruct.has_key(scs.HOP_EXCLUSION_TAG):
                            excludes = pathStruct[scs.HOP_EXCLUSION_TAG]
                        else:
                            excludes = []

                        # get hop URN
                        urn = hop.urn

                        # Add to the excludes list
                        if hop.excludeFromSCS:
                            excludes.append(urn)
                        elif hop.vlans_unavailable and len(hop.vlans_unavailable) > 0:
                            excludes.append(urn + "=" + str(hop.vlans_unavailable))

                        # Put the new objects in the struct
                        pathStruct[scs.HOP_EXCLUSION_TAG] = excludes
                        profile[path] = pathStruct

        # Exclude any hops given as an option from _all_ hops
        links = None
        if (self.opts.excludehop and len(self.opts.excludehop) > 0) or (self.opts.includehop and len(self.opts.includehop) > 0):
            links = requestDOM.getElementsByTagName(RSpecParser.LINK_TAG)
        if links and len(links) > 0:
            if not self.opts.excludehop:
                self.opts.excludehop = []
            if not self.opts.includehop:
                self.opts.includehop = []
            self.logger.debug("Got links and option to exclude hops: %s, include hops: %s", self.opts.excludehop, self.opts.includehop)
            for exclude in self.opts.excludehop:
                # For each path
                for link in links:
                    path = link.getAttribute(Link.CLIENT_ID_TAG)
                    path = str(path).strip()
                    if profile.has_key(path):
                        pathStruct = profile[path]
                    else:
                        pathStruct = {}

                    # Get hop_exclusion_list
                    if pathStruct.has_key(scs.HOP_EXCLUSION_TAG):
                        excludes = pathStruct[scs.HOP_EXCLUSION_TAG]
                    else:
                        excludes = []

                    excludes.append(exclude)
                    self.logger.debug("Excluding %s from path %s", exclude, path)

                    # Put the new objects in the struct
                    pathStruct[scs.HOP_EXCLUSION_TAG] = excludes
                    profile[path] = pathStruct

            for include in self.opts.includehop:
                # For each path
                for link in links:
                    path = link.getAttribute(Link.CLIENT_ID_TAG)
                    path = str(path).strip()
                    if profile.has_key(path):
                        pathStruct = profile[path]
                    else:
                        pathStruct = {}

                    # Get hop_inclusion_list
                    if pathStruct.has_key(scs.HOP_INCLUSION_TAG):
                        includes = pathStruct[scs.HOP_INCLUSION_TAG]
                    else:
                        includes = []

                    includes.append(include)
                    self.logger.debug("Including %s on path %s", include, path)

                    # Put the new objects in the struct
                    pathStruct[scs.HOP_INCLUSION_TAG] = includes
                    profile[path] = pathStruct

        if profile != {}:
            options[scs.GENI_PROFILE_TAG] = profile
        self.logger.debug("Sending SCS options %s", options)

        return requestDOM.toprettyxml(), options
        
    def parseSCSResponse(self, scsResponse):

        expandedRSpec = scsResponse.rspec()

        if self.opts.debug or self.opts.fakeModeDir:
            # Write the RSpec the SCS gave us to a file
            header = "<!-- SCS expanded stitching request for:\n\tSlice: %s\n -->" % (self.slicename)
            if expandedRSpec and is_rspec_string( expandedRSpec, None, None, logger=self.logger ):
                # This line seems to insert extra \ns - GCF ticket #202
#                content = getPrettyRSpec(expandedRSpec)
                content = stripBlankLines(string.replace(expandedRSpec, "\\n", '\n'))
            else:
                content = "<!-- No valid RSpec returned. -->"
                if expandedRSpec is not None:
                    content += "\n<!-- \n" + expandedRSpec + "\n -->"

            # Set -o to ensure this goes to a file, not logger or stdout
            opts_copy = copy.deepcopy(self.opts)
            opts_copy.output = True
            handler_utils._printResults(opts_copy, self.logger, header, \
                                            content, \
                                            Aggregate.FAKEMODESCSFILENAME)
            # In debug mode, keep copies of old SCS expanded requests
            if self.logger.isEnabledFor(logging.DEBUG):
                handler_utils._printResults(opts_copy, self.logger, header, content, Aggregate.FAKEMODESCSFILENAME + str(self.scsCalls))
            self.logger.debug("Wrote SCS expanded RSpec to %s", \
                                  Aggregate.FAKEMODESCSFILENAME)

            # A debugging block: print out the VLAN tag the SCS picked for each hop, independent of objects
            if self.logger.isEnabledFor(logging.DEBUG):
                start = 0
                while True:
                    if not content.find("<link id=", start) >= start:
                        break
                    hopIdStart = content.find('<link id=', start) + len('<link id=') + 1
                    hopIdEnd = content.find(">", hopIdStart)-1
                    # Print the link ID
                    hop = content[hopIdStart:hopIdEnd]
                    # find suggestedVLANRange
                    suggestedStart = content.find("suggestedVLANRange>", hopIdEnd) + len("suggestedVLANRange>")
                    suggestedEnd = content.find("</suggested", suggestedStart)
                    suggested = content[suggestedStart:suggestedEnd]
                    # print that
                    self.logger.debug("SCS gave hop %s suggested VLAN %s", hop, suggested)
                    start = suggestedEnd

       # parseRequest
        parsed_rspec = self.rspecParser.parse(expandedRSpec)
#        self.logger.debug("Parsed SCS expanded RSpec of type %r",
#                          type(parsed_rspec))

        # parseWorkflow
        workflow = scsResponse.workflow_data()
        scsResponse = None # once workflow extracted, done with that object
        if self.opts.debug:
            import pprint
            pp = pprint.PrettyPrinter(indent=2)
            pp.pprint(workflow)

        workflow_parser = WorkflowParser(self.logger)

        # Parse the workflow, creating Path/Hop/etc objects
        # In the process, fill in a tree of which hops depend on which,
        # and which AMs depend on which
        # Also mark each hop with what hop it imports VLANs from,
        # And check for AM dependency loops
        workflow_parser.parse(workflow, parsed_rspec)

        # FIXME: check each AM reachable, and we know the URL/API version to use

        # FIXME: Check SCS output consistency in a subroutine:
          # In each path: An AM with 1 hop must either _have_ dependencies or _be_ a dependency
          # All AMs must be listed in workflow data at least once per path they are in

        # Add extra info about the aggregates to the AM objects
        self.add_am_info(workflow_parser.aggs)

        if self.opts.debug:
            self.dump_objects(parsed_rspec, workflow_parser.aggs)

        return parsed_rspec, workflow_parser

    def add_am_info(self, aggs):
        '''Add extra information about the AMs to the Aggregate objects, like the API version'''
        options_copy = copy.deepcopy(self.opts)
        options_copy.debug = False
        options_copy.info = False

        for agg in aggs:
            # Don't do an aggregate twice
            if agg.urn in self.amURNsAddedInfo:
                continue

            # Note which AMs were user requested
            if agg.urn in self.parsedUserRequest.amURNs:
                agg.userRequested = True

# For using the test ION AM
#            if 'alpha.dragon' in agg.url:
#                agg.url =  'http://alpha.dragon.maxgigapop.net:12346/'

            # Query AM API versions supported, then set the maxVersion as a property on the aggregate
                # then use that to pick allocate vs createsliver
            # Check GetVersion geni_am_type contains 'dcn'. If so, set flag on the agg
            if options_copy.warn:
                omniargs = ['--ForceUseGetVersionCache', '-a', agg.url, 'getversion']
            else:
                omniargs = ['--ForceUseGetVersionCache', '-o', '--warn', '-a', agg.url, 'getversion']
                
            try:
                self.logger.debug("Getting extra AM info from Omni for AM %s", agg)
                logging.disable(logging.INFO)
                (text, version) = omni.call(omniargs, options_copy)
                logging.disable(logging.NOTSET)
                if isinstance (version, dict) and version.has_key(agg.url) and isinstance(version[agg.url], dict) \
                        and version[agg.url].has_key('value') and isinstance(version[agg.url]['value'], dict):
                    if version[agg.url]['value'].has_key('geni_am_type') and isinstance(version[agg.url]['value']['geni_am_type'], list):
                        if DCN_AM_TYPE in version[agg.url]['value']['geni_am_type']:
                            self.logger.debug("AM %s is DCN", agg)
                            agg.dcn = True
                    if version[agg.url]['value'].has_key('geni_api_versions') and isinstance(version[agg.url]['value']['geni_api_versions'], dict):
                        maxVer = 1
                        for key in version[agg.url]['value']['geni_api_versions'].keys():
                            if int(key) > maxVer:
                                maxVer = int(key)

                        # Hack alert: v3 AM implementations don't work even if they exist
                        if maxVer != 2:
                            self.logger.info("%s speaks AM API v%d, but sticking with v2", agg, maxVer)
#                        if self.opts.fakeModeDir:
#                            self.logger.warn("Testing v3 support")
#                            agg.api_version = 3
#                        agg.api_version = maxVer
            except:
                logging.disable(logging.NOTSET)

            # Remember we got the extra info for this AM
            self.amURNsAddedInfo.append(agg.urn)

    def dump_objects(self, rspec, aggs):
        '''Print out the hops, aggregates, and dependencies'''
        if rspec:
            stitching = rspec.stitching
            self.logger.debug( "\n===== Hops =====")
            for path in stitching.paths:
                self.logger.debug( "Path %s" % (path.id))
                if path.globalId:
                    self.logger.debug( "   GlobalId: %s" % path.globalId)
                for hop in path.hops:
                    self.logger.debug( "  Hop %s" % (hop))
                    # FIXME: don't use the private variable
                    self.logger.debug( "    VLAN Suggested (requested): %s" % (hop._hop_link.vlan_suggested_request))
                    self.logger.debug( "    VLAN Available Range (requested): %s" % (hop._hop_link.vlan_range_request))
                    if hop._hop_link.vlan_suggested_manifest:
                        self.logger.debug( "    VLAN Suggested (manifest): %s" % (hop._hop_link.vlan_suggested_manifest))
                    if hop._hop_link.vlan_range_manifest:
                        self.logger.debug( "    VLAN Available Range (manifest): %s" % (hop._hop_link.vlan_range_manifest))
                    if hop.vlans_unavailable and len(hop.vlans_unavailable) > 0:
                        self.logger.debug( "    VLANs found UN Available: %s" % hop.vlans_unavailable)
                    self.logger.debug( "    Import VLANs From: %s" % (hop.import_vlans_from))
                    deps = hop.dependsOn
                    if deps:
                        self.logger.debug( "    Dependencies:")
                        for h in deps:
                            self.logger.debug( "      Hop %s" % (h))

        if aggs and len(aggs) > 0:
            self.logger.debug( "\n===== Aggregates =====")
            for agg in aggs:
                self.logger.debug( "\nAggregate %s" % (agg))
                if agg.userRequested:
                    self.logger.debug("   (User requested)")
                else:
                    self.logger.debug("   (SCS added)")
                if agg.dcn:
                    self.logger.debug("   A DCN Aggregate")
                self.logger.debug("   Using AM API version %d", agg.api_version)
                if agg.manifestDom:
                    self.logger.debug("   Have a reservation here (%s)!", agg.url)
                if agg.pgLogUrl:
                    self.logger.debug("   PG Log URL %s", agg.pgLogUrl)
                for h in agg.hops:
                    self.logger.debug( "  Hop %s" % (h))
                for ad in agg.dependsOn:
                    self.logger.debug( "  Depends on %s" % (ad))

    def _raise_omni_error( self, msg, err=OmniError, triple=None ):
        msg2 = msg
        if triple is not None:
            msg2 += " "
            msg2 += str(triple)
        self.logger.error( msg2 )
        if triple is None:
            raise err, msg
        else: 
            raise err, (msg, triple)

    def combineManifests(self, ams, lastAM):
        '''Produce a single combined manifest string from the reservation results at each aggregate.
        lastAM is the last reservation that completed, for use as a template.'''
        # Nodes and hops come from the AM that owns those
        # interface_ref elements on link elements also come from the responsible AM
        # Top level link element is effectively arbitrary, but with comments on what other AMs said
        lastDom = lastAM.manifestDom
        combinedManifestDom = combineManifestRSpecs(ams, lastDom)
        manString = combinedManifestDom.toprettyxml()

        # set rspec to be UTF-8
        if isinstance(manString, unicode):
            manString = manString.encode('utf-8')
            self.logger.debug("Combined manifest RSpec was unicode")

        # FIXME
        # For fake mode this is really a request, but should be treating it as a manifest
        # For now, SCS gives us stitchSchemaV2 stuff, so rspeclint fails
        try:
            if self.opts.fakeModeDir:
                self.confirmGoodRSpec(manString, rspec_schema.REQUEST, False)
            else:
                self.confirmGoodRSpec(manString, rspec_schema.MANIFEST, False)
        except Exception, e:
            self.logger.error(e)

        return stripBlankLines(manString)

    def addExpiresAttribute(self, rspecDOM, sliceexp):
        '''Set the expires attribute on the rspec to the slice expiration. DCN AMs allocate the circuit only until then.'''
        if not rspecDOM:
            return
        if not sliceexp or str(sliceexp).strip() == "":
            return

        rspecs = rspecDOM.getElementsByTagName(RSpecParser.RSPEC_TAG)
        if not rspecs or len(rspecs) < 1:
            return

        if rspecs[0].hasAttribute(RSpecParser.EXPIRES_ATTRIBUTE):
            self.logger.debug("Not over-riding expires %s", rspecs[0].getAttribute(RSpecParser.EXPIRES_ATTRIBUTE))
            return
        rspecs[0].setAttribute(RSpecParser.EXPIRES_ATTRIBUTE, sliceexp.isoformat())
        self.logger.debug("Added expires %s", rspecs[0].getAttribute(RSpecParser.EXPIRES_ATTRIBUTE))
 
    def confirmSafeRequest(self):
        '''Confirm this request is not asking for a loop. Bad things should
        not be allowed, dangerous things should get a warning.'''

        # FIXME FIXME
        pass

    def saveAggregateState(self, oldAggs, newAggs):
        '''Save state from old aggregates for use with new aggregates from later SCS call'''
        for agg in newAggs:
            for oldAgg in oldAggs:
                if agg.urn == oldAgg.urn:
                    for hop in agg.hops:
                        for oldHop in oldAgg.hops:
                            if hop.urn == oldHop.urn:
                                if oldHop.excludeFromSCS:
                                    self.logger.warn("%s had been marked to exclude from SCS, but we got it again", oldHop)
                                hop.vlans_unavailable = hop.vlans_unavailable.union(oldHop.vlans_unavailable)
                                break

                    # FIXME: agg.allocateTries?
                    agg.dcn = oldAgg.dcn
                    agg.userRequested = oldAgg.userRequested
                    agg.api_version = oldAgg.api_version
                    break