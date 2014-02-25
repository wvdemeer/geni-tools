#!/usr/bin/env python

from __future__ import absolute_import

#----------------------------------------------------------------------
# Copyright (c) 2013-2014 Raytheon BBN Technologies
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

from .. import oscript as omni
from .util import OmniError, naiveUTC
from .util import credparsing as credutils
from .util.files import readFile
from .util import handler_utils

from . import stitch
from .stitch import defs
from .stitch.ManifestRSpecCombiner import combineManifestRSpecs
from .stitch.objects import Aggregate, Link, Node, LinkProperty
from .stitch.RSpecParser import RSpecParser
from .stitch import scs
from .stitch.workflow import WorkflowParser
from .stitch.utils import StitchingError, StitchingCircuitFailedError, stripBlankLines

from ..geni.util import rspec_schema
from ..geni.util.rspec_util import is_rspec_string, is_rspec_of_type, rspeclint_exists, validate_rspec, getPrettyRSpec

from ..sfa.util.xrn import urn_to_hrn, get_leaf

DCN_AM_TYPE = 'dcn' # geni_am_type value from AMs that use the DCN codebase
ORCA_AM_TYPE = 'orca' # geni_am_type value from AMs that use the Orca codebase
PG_AM_TYPE = 'protogeni' # geni_am_type / am_type from ProtoGENI based AMs

# Max # of times to call the stitching service
MAX_SCS_CALLS = 5

# File in which we save the slice cred so omni calls don't have to keep re-fetching it
# Valid substitutions: %username, %slicename, %slicehrn
SLICECRED_FILENAME = '/tmp/slice-%slicehrn-for-%username-cred.xml'

def urn_to_clean_hrn( urn ):
    hrn, type = urn_to_hrn( urn )
    hrn = handler_utils.remove_bad_characters( hrn )
    return hrn, type

# The main stitching class. Holds all the state about our attempt at doing stitching.
class StitchingHandler(object):
    '''Workhorse class to do stitching. See doStitching().'''

    def __init__(self, opts, config, logger):
        self.logger = logger
        config['logger'] = logger
        self.omni_config = config['omni']
        self.config = config
        self.parsedSCSRSpec = None
        self.lastException = None
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
                # Add -a options from the saved file, if none already supplied
                self.addAggregateOptions(args)
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
        self.rspecParser = RSpecParser(self.logger)
        self.parsedUserRequest = None
        if request:
            try:
                # read the rspec into a string, and add it to the rspecs dict
                requestString = handler_utils._derefRSpecNick(self, request)
            except Exception, exc:
                msg = "Unable to read rspec file '%s': %s" % (request, str(exc))
                if self.opts.devmode:
                    self.logger.warn(msg)
                else:
                    raise OmniError(msg)

            #    # Test if the rspec is really json containing an RSpec, and pull out the right thing
            #    requestString = amhandler.self._maybeGetRSpecFromStruct(requestString)

            # confirmGoodRequest
            self.confirmGoodRSpec(requestString)
            self.logger.debug("Valid GENI v3 request RSpec")
            
            # parseRequest
            self.parsedUserRequest = self.rspecParser.parse(requestString)
        else:
            raise OmniError("No request RSpec found, or slice name missing!")

        # If this is not a real stitching thing, just let Omni handle this.
        # This will also ensure each stitched link has an explicit capacity on 2 properties
        if not self.mustCallSCS(self.parsedUserRequest):
            self.logger.info("Not a stitching request - let Omni handle this.")

            if self.opts.noReservation:
                self.logger.info("Not reserving resources")
                raise StitchingError("Requested no reservation")

            # Warning: If this is createsliver and you specified multiple aggregates,
            # then omni only contacts 1 aggregate. That is likely not what you wanted.
            return omni.call(args, self.opts)

#        self.logger.debug("Edited request RSpec: %s", self.parsedUserRequest.getLinkEditedDom().toprettyxml())

        if self.opts.explicitRSpecVersion:
            self.logger.info("All manifest RSpecs will be in GENI v3 format")
            self.opts.explicitRSpecVersion = False
            self.opts.rspectype = ["GENI", '3']

        # FIXME: Confirm request is not asking for any loops
        self.confirmSafeRequest()

        # Remove any -a arguments from the opts so that when we later call omni
        # the right thing happens
        self.opts.aggregate = []

        # FIXME: Maybe use threading to parallelize confirmSliceOK and the 1st SCS call?

        # Get username for slicecred filename
        self.username = get_leaf(handler_utils._get_user_urn(self.logger, self.framework.config))
        if not self.username:
            raise OmniError("Failed to find your username to name your slice credential")

        # Ensure the slice is valid before all those Omni calls use it
        (sliceurn, sliceexp) = self.confirmSliceOK()

        # Here is where we used to add the expires attribute. No
        # longer necessary (or a good idea).

        self.scsService = scs.Service(self.opts.scsURL, self.opts.ssltimeout)
        self.scsCalls = 0

        # Compare the list of AMs in the request with AMs known
        # to the SCS. Any that the SCS does not know means the request
        # cannot succeed if those are AMs in a stitched link
#        self.checkSCSAMs()

        # Call SCS and then do reservations at AMs, deleting or retrying SCS as needed
        lvl = None
        try:
            # Passing in the request as a DOM - after allowing edits as necessary. OK?
            lastAM = self.mainStitchingLoop(sliceurn, self.parsedUserRequest.getLinkEditedDom())

            # Construct a unified manifest
            # include AMs, URLs, API versions
            if lastAM.isEG:
                self.logger.debug("Last AM was an EG AM. Find another for the template.")
                i = 1
                while lastAM.isEG and i <= len(self.ams_to_process):
                    # This has lost some hops and messed up hop IDs. Don't use it as the template
                    # I'd like to find another AM we did recently
                    lastAM = self.ams_to_process[-i]
                    i = i + 1
                if lastAM.isEG:
                    self.logger.debug("Still had an EG template AM?")
            combinedManifest = self.combineManifests(self.ams_to_process, lastAM)

            # FIXME: Handle errors. Maybe make return use code/value/output struct
            # If error and have an expanded rquest from SCS, include that in output.
            #   Or if particular AM had errors, ID the AMs and errors

            # FIXME: This prepends a header on an RSpec that might already have a header
            # -- maybe replace any existing header
            # FIXME: Without the -o option this is really verbose! Maybe set -o?
            ot = self.opts.output
            if not self.opts.tostdout:
                self.opts.output = True
            lvl = self.logger.getEffectiveLevel()
            self.logger.setLevel(logging.WARN)
            retVal, filename = handler_utils._writeRSpec(self.opts, self.logger, combinedManifest, self.slicename, 'stitching-combined', '', None)
            self.logger.setLevel(lvl)
            self.opts.output = ot
            if filename:
                self.logger.info("Saved combined reservation RSpec at %d AMs to file %s", len(self.ams_to_process), filename)

        except StitchingError, se:
            if lvl:
                self.logger.setLevel(lvl)
            # FIXME: Return anything different for stitching error?
            # Do we want to return a geni triple struct?
            if self.lastException:
                self.logger.error("Root cause error: %s", self.lastException)
                newError = StitchingError("%s which caused %s" % (str(self.lastException), str(se)))
                se = newError
            raise se
        finally:
            # Save a file with the aggregates used in this slice
            self.saveAggregateList(sliceurn)

            # Clean up temporary files
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

    # Compare the list of AMs in the request with AMs known
    # to the SCS. Any that the SCS does not know means the request
    # cannot succeed if those are AMs in a stitched link
    def checkSCSAMs(self):
        # FIXME: This takes time. If this can't block a more expensive later operation, why bother?
        scsAggs = {}
        try:
            scsAggs = self.scsService.ListAggregates(False, self.opts.ssltimeout)
        except Exception, e:
            self.logger.debug("SCS ListAggregates failed: %s", e)
        if scsAggs and isinstance(scsAggs, dict) and len(scsAggs.keys()) > 0:
            if scsAggs.has_key('value') and scsAggs['value'].has_key('geni_aggregate_list'):
                scsAggs = scsAggs['value']['geni_aggregate_list']
#                self.logger.debug("Got geni_agg_list from scs: %s", scsAggs)
                # Now sanity check AMs requested
                # Note that this includes AMs that the user does not
                # want to stitch - so we cannot error out early
                # FIXME: Can we ID from the request which are AMs that need a stitch?
                for reqAMURN in self.parsedUserRequest.amURNs:
                    found = False
                    for sa in scsAggs.keys():
                        if scsAggs[sa]['urn'] == reqAMURN:
                            self.logger.debug("Requested AM URN %s is listed by SCS with URL %s", reqAMURN, scsAggs[sa]['url'])
                            found = True
                            break
                    if not found:
                        self.logger.warn("Your request RSpec specifies the aggregate (component manager) '%s' for which there are no stitching paths configured. If you requested a stitched link to this aggregate, it will fail.", reqAMURN)


    def cleanup(self):
        '''Remove temporary files if not in debug mode'''
        if self.opts.debug:
            return
        
        if os.path.exists(Aggregate.FAKEMODESCSFILENAME):
            os.unlink(Aggregate.FAKEMODESCSFILENAME)

        if self.savedSliceCred and os.path.exists(self.opts.slicecredfile):
            os.unlink(self.opts.slicecredfile)

        if not self.ams_to_process:
            return

        for am in self.ams_to_process:
            # Remove getversion files
            filename = handler_utils._construct_output_filename(self.opts, None, am.url, None, "getversion", ".json", 1)
            if os.path.exists(filename):
                os.unlink(filename)

            # Remove any RSpec
            if am.rspecfileName and not self.opts.output:
                if os.path.exists(am.rspecfileName):
                    os.unlink(am.rspecfileName)

    def mainStitchingLoop(self, sliceurn, requestDOM, existingAggs=None):
        # existingAggs are Aggregate objects
        self.scsCalls = self.scsCalls + 1
        if self.scsCalls > 1:
            thStr = 'th'
            if self.scsCalls == 2 or self.scsCalls == 3:
                thStr = 'rd'
            if self.scsCalls == self.maxSCSCalls:
                self.logger.info("Calling SCS for the %d%s and last time...", self.scsCalls, thStr)
            else:
                self.logger.info("Calling SCS for the %d%s time...", self.scsCalls, thStr)

        scsResponse = self.callSCS(sliceurn, requestDOM, existingAggs)
        self.lastException = None # Clear any last exception from the last run through

        if self.scsCalls > 1 and existingAggs:
            # We are doing another call.
            # Let AMs recover. Is this long enough?
            # If one of the AMs is a DCN AM, use that sleep time instead - longer
            sTime = Aggregate.PAUSE_FOR_AM_TO_FREE_RESOURCES_SECS
            for agg in existingAggs:
                if agg.dcn:
                    sTime = Aggregate.PAUSE_FOR_DCN_AM_TO_FREE_RESOURCES_SECS
                    break
            self.logger.info("Pausing for %d seconds for Aggregates to free up resources...\n\n", sTime)
            time.sleep(sTime)

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

        self.logger.debug("SCS workflow said to include resources from these aggregates:")
        for am in self.ams_to_process:
            self.logger.debug("\t%s", am)

        addedAMs = []
        for amURN in self.parsedSCSRSpec.amURNs:
#            self.logger.debug("Looking at SCS returned amURN %s", amURN)
            found = False
            for agg in self.ams_to_process:
                if agg.urn == amURN:
                    found = True
#                    self.logger.debug(" .. was already in ams_to_process")
                    break
                # For EG there are multiple URNs that are really the same
                # If find one, found them all
                for urn2 in agg.urn_syns:
                    if urn2 == amURN:
#                        self.logger.debug(" .. was in ams_to_process under synonym. Ams_to_process had %s", agg.urn)
                        found = True
                        break
            if found:
                continue
            else:
                am = Aggregate.find(amURN)
                addedAMs.append(am)
                if not am.url:
                    # Try to pull from agg nicknames in the omni_config
                    for (amURNNick, amURLNick) in self.config['aggregate_nicknames'].values():
                        if amURNNick and amURNNick.strip() in am.urn_syns and amURLNick.strip() != '':
                            am.url = amURLNick
                            self.logger.debug("Found AM %s URL from omni_config AM nicknames: %s", amURN, am.url)
                            break

                if not am.url:
                    # Try asking our CH for AMs to get the URL for the
                    # given URN
                    fw_ams = dict()
                    try:
                        fw_ams = self.framework.list_aggregates()
                        for fw_am_urn in fw_ams.keys():
                            if fw_am_urn and fw_am_urn.strip() in am.urn_syns and fw_ams[fw_am_urn].strip() != '':
                                am.url = fw_ams[fw_am_urn]
                                self.logger.debug("Found AM %s URL from CH ListAggs: %s", amURN, am.url)
                                break
                    except:
                        pass
                if not am.url:
                    self.logger.error("RSpec requires AM %s which is not in workflow and URL is unknown!", amURN)
                else:
                    self.logger.debug("Adding am to ams_to_process from URN %s, with url %s", amURN, am.url)
                    self.ams_to_process.append(am)
        # Done adding user requested non linked AMs to list of AMs to
        # process

        # Add extra info about the aggregates to the AM objects
        self.add_am_info(self.ams_to_process)

        # FIXME: check each AM reachable, and we know the URL/API version to use

        if self.opts.debug:
            self.dump_objects(self.parsedSCSRSpec, self.ams_to_process)

        self.logger.info("Stitched reservation will include resources from these aggregates:")
        for am in self.ams_to_process:
            self.logger.info("\t%s", am)

        # If we said this rspec needs a fixed / fake endpoint, add it here - so the SCS and other stuff
        # doesn't try to do anything with it
        if self.opts.fixedEndpoint:
            self.addFakeNode()

        if self.opts.noReservation:
            self.logger.info("Not reserving resources")
            # Write the request rspec to a string that we save to a file
            requestString = self.parsedSCSRSpec.dom.toxml(encoding="utf-8")
            header = "<!-- Expanded Resource request for stitching for:\n\tSlice: %s -->" % (self.slicename)
            content = stripBlankLines(string.replace(requestString, "\\n", '\n'))
            rspecfileName = "%s-expanded-request.xml" % self.slicename

            # Set -o to ensure this request RSpec goes to a file, not logger or stdout
            opts_copy = copy.deepcopy(self.opts)
            opts_copy.output = True
            lvl = self.logger.getEffectiveLevel()
            self.logger.setLevel(logging.WARN)
            handler_utils._printResults(opts_copy, self.logger, header, content, rspecfileName)
            self.logger.setLevel(lvl)
            self.logger.info("Saved expanded request RSpec to file %s", rspecfileName)
            raise StitchingError("Requested no reservation")

        # The launcher handles calling the aggregates to do their allocation
        launcher = stitch.Launcher(self.opts, self.slicename, self.ams_to_process)
        try:
            # Spin up the main loop
            lastAM = launcher.launch(self.parsedSCSRSpec, self.scsCalls)
# for testing calling the SCS only many times
#            raise StitchingCircuitFailedError("testing")

        except StitchingCircuitFailedError, se:
            self.lastException = se
            if self.scsCalls == self.maxSCSCalls:
                self.logger.error("Stitching max circuit failures reached - will delete and exit.")
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

            # construct new SCS args
            # redo SCS call et al
            # FIXME: aggs.hops have loose tag: mark the hops in the request as explicitly loose
            # FIXME: Here we pass in the request to give to the SCS. I'd like this
            # to be modified (different VLAN range? Some hops marked loose?) in future
            lastAM = self.mainStitchingLoop(sliceurn, requestDOM, aggs)
        except StitchingError, se:
            self.logger.error("Stitching failed with an error: %s", se)
            if self.lastException:
                self.logger.error("Root cause error: %s", self.lastException)
                newError = StitchingError("%s which caused %s" % (str(self.lastException), str(se)))
                se = newError
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

        self.logger.info("Checking that slice %s is valid...", self.slicename)

        # Get slice URN from name
        try:
            sliceurn = self.framework.slice_name_to_urn(self.slicename)
        except Exception, e:
            self.logger.error("Could not determine slice URN from name %s: %s", self.slicename, e)
            raise StitchingError(e)

        self.slicehrn = urn_to_clean_hrn(sliceurn)[0]

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

        self.savedSliceCred = False
        # Force the slice cred to be from a saved file if not already set
        if not self.opts.slicecredfile:
            self.opts.slicecredfile = SLICECRED_FILENAME
            if "%username" in self.opts.slicecredfile:
                self.opts.slicecredfile = string.replace(self.opts.slicecredfile, "%username", self.username)
            if "%slicename" in self.opts.slicecredfile:
                self.opts.slicecredfile = string.replace(self.opts.slicecredfile, "%slicename", self.slicename)
            if "%slicehrn" in self.opts.slicecredfile:
                self.opts.slicecredfile = string.replace(self.opts.slicecredfile, "%slicehrn", self.slicehrn)
            trim = -4
            if self.opts.slicecredfile.endswith("json"):
                trim = -5
            # -4 is to cut off .xml. It would be -5 if the cred is json
            handler_utils._save_cred(self, self.opts.slicecredfile[:trim], slicecred)
            self.savedSliceCred = True

        # Ensure slice not expired
        sliceexp = credutils.get_cred_exp(self.logger, slicecred)
        sliceexp = naiveUTC(sliceexp)
        now = datetime.datetime.utcnow()
        shorthours = 3
        middays = 1
        if sliceexp <= now:
            # FIXME: Maybe if the slice doesn't exist, create it?
            # omniargs = ["createslice", self.slicename]
            # try:
            #     (slicename, message) = omni.call(omniargs, self.opts)
            # except:
            #     pass
            raise StitchingError("Slice %s expired at %s" % (sliceurn, sliceexp))
        elif sliceexp - datetime.timedelta(hours=shorthours) <= now:
            self.logger.warn('Slice %s expires in <= %d hours on %s UTC' % (sliceurn, shorthours, sliceexp))
            self.logger.debug('It is now %s UTC' % (datetime.datetime.utcnow()))
        elif sliceexp - datetime.timedelta(days=middays) <= now:
            self.logger.info('Slice %s expires within %d day on %s UTC' % (sliceurn, middays, sliceexp))
        else:
            self.logger.info('Slice %s expires on %s UTC' % (sliceurn, sliceexp))

        # return the slice urn, slice expiration (datetime)
        return (sliceurn, sliceexp)

    # Ensure the link has 2 well formed property elements each with a capacity
    def addCapacityOneLink(self, link):
        # look for property elements
        if len(link.properties) > 2:
            raise StitchingError("Your request RSpec is malformed: include either 2 or 0 property elements on link '%s'" % link.id)
        # Get the 2 node IDs
        ifcs = link.interfaces
        if len(ifcs) < 2:
            self.logger.debug("Link '%s' doesn't have at least 2 interfaces? Has %d", link.id, len(ifcs))
            return
        if len(ifcs) > 2:
            self.logger.debug("Link '%s' has more than 2 interfaces (%d). Picking source and dest from the first 2 on different AMs.", link.id, len(ifcs))
        node1ID = ifcs[0].client_id
        node1AM = None
        for node in self.parsedUserRequest.nodes:
            if node1ID in node.interface_ids:
                node1AM = node.amURN
                break

        # Now find a 2nd interface on a different AM
        node2ID = None
        node2AM = None
        for ifc in ifcs:
            if ifc.client_id == node1ID:
                continue
            node2ID = ifc.client_id
            node2AM = None
            for node in self.parsedUserRequest.nodes:
                if node2ID in node.interface_ids:
                    node2AM = node.amURN
                    break
            if node2AM == node1AM:
                node2ID = None
                node2AM = None
                continue
            else:
                break
        if node2AM is None:
            # No 2nd interface on different AM found
            self.logger.debug("Link '%s' doesn't have interfaces on more than 1 AM ('%s')?" % (link.id, node1AM))
        else:
            self.logger.debug("Link '%s' properties will be from '%s' to '%s'", link.id, node1ID, node2ID)

        # If there are no property elements
        if len(link.properties) == 0:
            self.logger.debug("Link %s had no properties - must add them", link.id)
            # Then add them
            s_id = node1ID
            d_id = node2ID
            s_p = LinkProperty(s_id, d_id, None, None, self.opts.defaultCapacity)
            s_p.link = link
            d_p = LinkProperty(d_id, s_id, None, None, self.opts.defaultCapacity)
            d_p.link = link
            link.properties = [s_p, d_p]
            return

        # If the elements are there, error check them, adding property if necessary
        if len(link.properties) == 2:
            props = link.properties
            prop1S = props[0].source_id
            prop1D = props[0].dest_id
            prop2S = props[1].source_id
            prop2D = props[1].dest_id
            if prop1S is None or prop1S == "":
                raise StitchingError("Malformed property on link %s missing source_id attribute" % link.id)
            if prop1D is None or prop1D == "":
                raise StitchingError("Malformed property on link %s missing dest_id attribute" % link.id)
            if prop1D == prop1S:
                raise StitchingError("Malformed property on link %s has matching source and dest_id: %s" % (link.id, prop1D))
            if prop2S is None or prop2S == "":
                raise StitchingError("Malformed property on link %s missing source_id attribute" % link.id)
            if prop2D is None or prop2D == "":
                raise StitchingError("Malformed property on link %s missing dest_id attribute" % link.id)
            if prop2D == prop2S:
                raise StitchingError("Malformed property on link %s has matching source and dest_id: %s" % (link.id, prop2D))
            # FIXME: Compare to the interface_refs
            if prop1S != prop2D or prop1D != prop2S:
                raise StitchingError("Malformed properties on link %s: source and dest tags are not reversed" % link.id)
            if props[0].capacity and not props[1].capacity:
                props[1].capacity = props[0].capacity
            if props[1].capacity and not props[0].capacity:
                props[0].capacity = props[1].capacity
            for prop in props:
                if prop.capacity is None or prop.capacity == "":
                    prop.capacity = self.opts.defaultCapacity
                # FIXME: Warn about really small or big capacities?
            return

        # There is a single property tag
        prop = link.properties[0]
        if prop.source_id is None or prop.source_id == "":
            raise StitchingError("Malformed property on link %s missing source_id attribute" % link.id)
        if prop.dest_id is None or prop.dest_id == "":
            raise StitchingError("Malformed property on link %s missing dest_id attribute" % link.id)
        if prop.dest_id == prop.source_id:
            raise StitchingError("Malformed property on link %s has matching source and dest_id: %s" % (link.id, prop.dest_id))
        # FIXME: Compare to the interface_refs
        if prop.capacity is None or prop.capacity == "":
            prop.capacity = self.opts.defaultCapacity
        # FIXME: Warn about really small or big capacities?
        # Create the 2nd property with the source and dest reversed
        prop2 = LinkProperty(prop.dest_id, prop.source_id, prop.latency, prop.packet_loss, prop.capacity)
        link.properties = [prop, prop2]
        self.logger.debug("Link %s added missing reverse property")

    # Ensure all implicit AMs (from interface_ref->node->component_manager_id) are explicit on the link
    def ensureLinkListsAMs(self, link, requestRSpecObject):
        if not link:
            return

        ams = []
        for ifc in link.interfaces:
            found = False
            for node in requestRSpecObject.nodes:
                if ifc.client_id in node.interface_ids:
                    if node.amURN not in ams:
                        ams.append(node.amURN)
                    found = True
                    self.logger.debug("Link %s interface %s found on node %s", link.id, ifc.client_id, node.id)
                    break
            if not found:
                self.logger.debug("Link %s interface %s not found on any node", link.id, ifc.client_id)
                # FIXME: What would this mean?

        for amURN in ams:
            am = Aggregate.find(amURN)
            if am not in link.aggregates:
                self.logger.debug("Adding missing AM %s to link %s", amURN, link.id)
                link.aggregates.append(am)

    def mustCallSCS(self, requestRSpecObject):
        '''Does this request actually require stitching?
        Check: >=1 link in main body with >= 2 diff component_manager
        names and no shared_vlan extension and no non-VLAN link_type
        '''
        if requestRSpecObject:
            for link in requestRSpecObject.links:
                # Make sure this link explicitly lists all its aggregates, so this test is valid
                self.ensureLinkListsAMs(link, requestRSpecObject)
                if len(link.aggregates) > 1 and not link.hasSharedVlan and link.typeName == link.VLAN_LINK_TYPE:
                    # Ensure this link has 2 well formed property elements with explicity capacities
                    self.addCapacityOneLink(link)
                    return True

            # FIXME: Can we be robust to malformed requests, and stop and warn the user?
                # EG the link has 2+ interface_ref elements that are on 2+ nodes belonging to 2+ AMs?
                # Currently the parser only saves the IRefs on Links - no attempt to link to Nodes
                # And for Nodes, we don't even look at the Interface sub-elements

        return False

    def callSCS(self, sliceurn, requestDOM, existingAggs):
        '''Construct SCS args, call the SCS service'''

        requestString, scsOptions = self.constructSCSArgs(requestDOM, existingAggs)
        existingAggs = None # Clear to note we are done
        self.scsService.result = None # Avoid any unexpected issues

        self.logger.debug("Calling SCS with options %s", scsOptions)
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
        # If you append '=<VLANRange>' to the hop URN, that means to exclude
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
            links = requestDOM.getElementsByTagName(defs.LINK_TAG)
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

        return requestDOM.toprettyxml(encoding="utf-8"), options
        
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

        # FIXME: Check SCS output consistency in a subroutine:
          # In each path: An AM with 1 hop must either _have_ dependencies or _be_ a dependency
          # All AMs must be listed in workflow data at least once per path they are in

        return parsed_rspec, workflow_parser

    def add_am_info(self, aggs):
        '''Add extra information about the AMs to the Aggregate objects, like the API version'''
        options_copy = copy.deepcopy(self.opts)
        options_copy.debug = False
        options_copy.info = False

        aggsc = copy.copy(aggs)

        for agg in aggsc:
            # Don't do an aggregate twice
            if agg.urn in self.amURNsAddedInfo:
                continue
#            self.logger.debug("add_am_info looking at %s", agg)

            # Note which AMs were user requested
            if agg.urn in self.parsedUserRequest.amURNs:
                agg.userRequested = True
            else:
                for urn2 in agg.urn_syns:
                    if urn2 in self.parsedUserRequest.amURNs:
                        agg.userRequested = True

            # FIXME: Better way to detect this?
            if handler_utils._extractURL(self.logger, agg.url) in defs.EXOSM_URL:
                agg.isExoSM = True
#                self.logger.debug("%s is the ExoSM cause URL is %s", agg, agg.url)

            # EG AMs in particular have 2 URLs in some sense - ExoSM and local
            # So note the other one, since VMs are split between the 2
            for (amURN, amURL) in self.config['aggregate_nicknames'].values():
                if amURN.strip() in agg.urn_syns:
                    hadURL = handler_utils._extractURL(self.logger, agg.url)
                    newURL = handler_utils._extractURL(self.logger, amURL)
                    if hadURL != newURL and not hadURL in newURL and not newURL in hadURL and not newURL.strip == '':
                        agg.alt_url = newURL
                        break
#                    else:
#                        self.logger.debug("Not setting alt_url for %s. URL is %s, alt candidate was %s with URN %s", agg, hadURL, newURL, amURN)
#                elif "exogeni" in amURN and "exogeni" in agg.urn:
#                    self.logger.debug("Config had URN %s URL %s, but that URN didn't match our URN synonyms for %s", amURN, newURL, agg)

            if "exogeni" in agg.urn and not agg.alt_url:
#                self.logger.debug("No alt url for Orca AM %s (URL %s) with URN synonyms:", agg, agg.url)
#                for urn in agg.urn_syns:
#                    self.logger.debug("\t%s", urn)
                if not agg.isExoSM:
                    agg.alt_url = defs.EXOSM_URL

            # Try to get a URL from the CH? Do we want/need this
            # expense? This is a call to the CH....
            # Comment this out - takes too long, not clear
            # it is needed.
#            if not agg.alt_url:
#                fw_ams = dict()
#                try:
#                    fw_ams = self.framework.list_aggregates()
#                    for fw_am_urn in fw_ams.keys():
#                        if fw_am_urn and fw_am_urn.strip() in am.urn_syns and fw_ams[fw_am_urn].strip() != '':
#                            cand_url = fw_ams[fw_am_urn]
#                            if cand_url != am.url and not am.url in cand_url and not cand_url in am.url:
#                                am.alt_url = cand_url
#                                self.logger.debug("Found AM %s alternate URL from CH ListAggs: %s", am.urn, am.alt_url)
#                                break
#                except:
#                    pass

            if agg.isExoSM and agg.alt_url and self.opts.noExoSM:
                self.logger.warn("%s used ExoSM URL. Changing to %s", agg, agg.alt_url)
                amURL = agg.url
                agg.url = agg.alt_url
                agg.alt_url = amURL
                agg.isExoSM = False

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
                        elif ORCA_AM_TYPE in version[agg.url]['value']['geni_am_type']:
                            self.logger.debug("AM %s is Orca", agg)
                            agg.isEG = True
                        elif PG_AM_TYPE in version[agg.url]['value']['geni_am_type']:
                            self.logger.debug("AM %s is ProtoGENI", agg)
                            agg.isPG = True
                    elif version[agg.url]['value'].has_key('geni_am_type') and ORCA_AM_TYPE in version[agg.url]['value']['geni_am_type']:
                            self.logger.debug("AM %s is Orca", agg)
                            agg.isEG = True
                    # This code block looks nice but doesn't work - the version object is not the full triple
#                    elif version[agg.url].has_key['code'] and isinstance(version[agg.url]['code'], dict) and \
#                            version[agg.url]['code'].has_key('am_type') and str(version[agg.url]['code']['am_type']).strip() != "":
#                        if version[agg.url]['code']['am_type'] == PG_AM_TYPE:
#                            self.logger.debug("AM %s is ProtoGENI", agg)
#                            agg.isPG = True
#                        elif version[agg.url]['code']['am_type'] == ORCA_AM_TYPE:
#                            self.logger.debug("AM %s is Orca", agg)
#                            agg.isEG = True
#                        elif version[agg.url]['code']['am_type'] == DCN_AM_TYPE:
#                            self.logger.debug("AM %s is DCN", agg)
#                            agg.dcn = True
                    if version[agg.url]['value'].has_key('geni_api_versions') and isinstance(version[agg.url]['value']['geni_api_versions'], dict):
                        maxVer = 1
                        hasV2 = False
                        for key in version[agg.url]['value']['geni_api_versions'].keys():
                            if int(key) == 2:
                                hasV2 = True
                                # Ugh. Why was I changing the URL based on the Ad? Not needed, Omni does this.
                                # And if the AM says the current URL is the current opts.api_version OR the AM only lists 
                                # one URL, then changing the URL makes no sense. So if I later decide I need this
                                # for some reason, only do it if len(keys) > 1 and [value][geni_api] != opts.api_version
                                # Or was I trying to change to the 'canonical' URL for some reason?
#                                # Change the stored URL for this Agg to the URL the AM advertises if necessary
#                                if agg.url != version[agg.url]['value']['geni_api_versions'][key]:
#                                    agg.url = version[agg.url]['value']['geni_api_versions'][key]
                            if int(key) > maxVer:
                                maxVer = int(key)

                        # Stitcher doesn't really know how to parse
                        # APIv1 return structs
                        if maxVer == 1:
                            msg = "%s speaks only AM API v1 - not supported!" % agg
                            #self.logger.error(msg)
                            raise StitchingError(msg)
                        # Hack alert: v3 AM implementations don't work even if they exist
                        if not hasV2:
                            msg = "%s does not speak AM API v2 (max is V%d). APIv2 required!" % (agg, maxVer)
                            #self.logger.error(msg)
                            raise StitchingError(msg)
#                        if maxVer != 2:
#                            self.logger.debug("%s speaks AM API v%d, but sticking with v2", agg, maxVer)

#                        if self.opts.fakeModeDir:
#                            self.logger.warn("Testing v3 support")
#                            agg.api_version = 3
#                        agg.api_version = maxVer
            except StitchingError, se:
                # FIXME: Return anything different for stitching error?
                # Do we want to return a geni triple struct?
                raise
            except:
                pass
            finally:
                logging.disable(logging.NOTSET)

            if agg.isEG and self.opts.useExoSM and not agg.isExoSM:
                agg.alt_url = defs.EXOSM_URL
                self.logger.info("%s is an EG AM and user asked for ExoSM. Changing to %s", agg, agg.alt_url)
                amURL = agg.url
                agg.url = agg.alt_url
                agg.alt_url = amURL
                agg.isExoSM = True
                aggsc.append(agg)
                continue
#            else:
#                self.logger.debug("%s is EG: %s, alt_url: %s, isExo: %s", agg, agg.isEG, agg.alt_url, agg.isExoSM)

            # Save off the aggregate nickname if possible
            agg.nick = handler_utils._lookupAggNick(self, agg.url)

 #           self.logger.debug("Remembering done getting extra info for %s", agg)

            # Remember we got the extra info for this AM
            self.amURNsAddedInfo.append(agg.urn)
        # Done loop over aggs

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
                if agg.isPG:
                    self.logger.debug("   A ProtoGENI Aggregate")
                if agg.isEG:
                    self.logger.debug("   An Orca Aggregate")
                if agg.isExoSM:
                    self.logger.debug("   The ExoSM Aggregate")
                if agg.alt_url:
                    self.logger.debug("   Alternate URL: %s", agg.alt_url)
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
        manString = combinedManifestDom.toprettyxml(encoding="utf-8")

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

    def saveAggregateList(self, sliceurn):
        '''Save a file with the list of aggregates used. Used as input
        to later stitcher calls, e.g. to delete from all AMs.'''
        # URN to hrn
        (slicehrn, stype) = urn_to_clean_hrn(sliceurn)
        if not slicehrn or slicehrn.strip() == '' or not stype=='slice':
            self.logger.warn("Couldn't parse slice HRN from URN %s",
                             sliceurn)
            return
        # ./$slicehrn-amlist.txt
        fname = "%s-amlist.txt" % slicehrn
        if not self.ams_to_process or len(self.ams_to_process) == 0:
            self.logger.debug("No AMs in AM list to process, so not creating amlist file")
            return

        # URL,URN
        with open (fname, 'w') as file:
            file.write("# AM List for stitched slice %s\n" % sliceurn)
            file.write("# Slice allocated at %s\n" % datetime.datetime.utcnow().isoformat())
            for am in self.ams_to_process:
                file.write("%s,%s\n" % (am.url, am.urn) )
                # Include am.userRequested? am.api_version? len(am._hops)?
#                file.write("%s,%s,%s,%d,%d\n" % (am.url, am.urn, am.userRequested,
#                           am.api_version, len(am._hops)))


    def addAggregateOptions(self, args):
        '''Read a file with a list of aggregates, adding those as -a
        options. Allows stitcher to delete from all AMs. Note that
        extra aggregate options are added only if no -a options are
        already supplied.'''
        # Find slice name from args[1]
        if not args or len(args) < 2:
            self.logger.debug("Cannot find slice name")
            return
        slicename = args[1]

        # get slice URN
        # Get slice URN from name
        try:
            sliceurn = self.framework.slice_name_to_urn(slicename)
        except Exception, e:
            self.logger.warn("Could not determine slice URN from name %s: %s", slicename, e)
            return

        if not sliceurn or sliceurn.strip() == '':
            self.logger.warn("Could not determine slice URN from name %s", slicename)
            return

        # get slice HRN
        (slicehrn, stype) = urn_to_clean_hrn(sliceurn)
        if not slicehrn or slicehrn.strip() == '' or not stype=='slice':
            self.logger.warn("Couldn't parse slice HRN from URN %s",
                             sliceurn)
            return

        # ./$slicehrn-amlist.txt
        fname = "%s-amlist.txt" % slicehrn

        # look to see if $slicehrn-amlist.txt exists
        if not os.path.exists(fname) or not os.path.getsize(fname) > 0:
            self.logger.debug("File of AMs for slice %s not found or empty: %s", slicename, fname)
            return

        self.logger.info("Reading stitching slice %s aggregates from file %s", slicename, fname)

        self.opts.ensure_value('aggregate', [])
        addOptions = True
        if len(self.opts.aggregate) > 0:
            addOptions = False
        with open(fname, 'r') as file:
        # For each line:
            for line in file:
                line = line.strip()
                # Skip if starts with # or is empty
                if line == '' or line.startswith('#'):
                    continue
                # split on ,
                (url,urn) = line.split(',')
#                (url,urn,userRequested,api_version,numHops) = line.split(',')
                url = url.strip()
                # If first looks like a URL, log
                if not url == '':
                    # add -a option
                    # Note this next doesn't avoid the dup of a nickname
                    if not url in self.opts.aggregate:
                        if addOptions:
                            self.logger.info("Adding aggregate option %s (%s)", url, urn)
                            self.opts.aggregate.append(url)
                        else:
                            self.logger.info("NOTE not adding aggregate %s", url)

    def addExpiresAttribute(self, rspecDOM, sliceexp):
        '''Set the expires attribute on the rspec to the slice
        expiration. DCN AMs used to not support renew, but this is no
        longer true, so this should not be necessary. Additionally,
        some AMs treat this as a strict requirement and if this
        exceeds local policy for maximum sliver, the request will fail.'''
        if not rspecDOM:
            return
        if not sliceexp or str(sliceexp).strip() == "":
            return

        rspecs = rspecDOM.getElementsByTagName(defs.RSPEC_TAG)
        if not rspecs or len(rspecs) < 1:
            return

        if rspecs[0].hasAttribute(defs.EXPIRES_ATTRIBUTE):
            self.logger.debug("Not over-riding expires %s", rspecs[0].getAttribute(defs.EXPIRES_ATTRIBUTE))
            return

        # Some PG based AMs cannot handle fractional seconds, and
        # erroneously treat expires as in local time. So (a) avoid
        # microseconds, and (b) explicitly note this is in UTC.
        # So this is sliceexp.isoformat() except without the
        # microseconds and with the Z. Note that PG requires exactly
        # this format.
        rspecs[0].setAttribute(defs.EXPIRES_ATTRIBUTE, sliceexp.strftime('%Y-%m-%dT%H:%M:%SZ'))
        self.logger.debug("Added expires %s", rspecs[0].getAttribute(defs.EXPIRES_ATTRIBUTE))
 
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
                    agg.isPG = oldAgg.isPG
                    agg.isEG = oldAgg.isEG
                    agg.isExoSM = oldAgg.isExoSM
                    agg.userRequested = oldAgg.userRequested
                    agg.alt_url = oldAgg.alt_url
                    agg.api_version = oldAgg.api_version
                    agg.nick = oldAgg.nick
                    break

    # If we said this rspec needs a fake endpoint, add it here - so the SCS and other stuff
    # doesn't try to do anything with it
    def addFakeNode(self):
        fakeNode = self.parsedSCSRSpec.dom.createElement(defs.NODE_TAG)
        fakeInterface = self.parsedSCSRSpec.dom.createElement("interface")
        fakeInterface.setAttribute(Node.CLIENT_ID_TAG, "fake:if0")
        fakeNode.setAttribute(Node.CLIENT_ID_TAG, "fake")
        fakeNode.setAttribute(Node.COMPONENT_MANAGER_ID_TAG, "urn:publicid:IDN+fake+authority+am")
        fakeCM = self.parsedSCSRSpec.dom.createElement(Link.COMPONENT_MANAGER_TAG)
        fakeCM.setAttribute(Link.NAME_TAG, "urn:publicid:IDN+fake+authority+am")
        fakeNode.appendChild(fakeInterface)
        fakeiRef = self.parsedSCSRSpec.dom.createElement(Link.INTERFACE_REF_TAG)
        fakeiRef.setAttribute(Node.CLIENT_ID_TAG, "fake:if0")
        # Find the rspec element from parsedSCSRSpec.dom
        rspecs = self.parsedSCSRSpec.dom.getElementsByTagName(defs.RSPEC_TAG)
        if rspecs and len(rspecs):
            rspec = rspecs[0]
            # Add a node to the dom
            self.logger.info("Adding fake Node endpoint")
            rspec.appendChild(fakeNode)

            # Also find all links and add an interface_ref
            for child in rspec.childNodes:
                if child.localName == defs.LINK_TAG:
                    linkName = child.getAttribute(Node.CLIENT_ID_TAG)
                    ifcCount = 0
                    ifcAMCount = 0 # Num AMs the interfaces are at
                    propCount = 0
                    ifc1Name = None
                    ifcAuths = []
                    for c2 in child.childNodes:
                        if c2.localName == Link.INTERFACE_REF_TAG:
                            ifcCount += 1
                            ifc1Name = c2.getAttribute(Node.CLIENT_ID_TAG)
                            for node in self.parsedSCSRSpec.nodes:
                                if ifc1Name in node.interface_ids:
                                    ifcAuth = node.amURN
                                    if not ifcAuth in ifcAuths:
                                        ifcAuths.append(ifcAuth)
                                        ifcAMCount += 1
                                    break
                        if c2.localName == Link.PROPERTY_TAG:
                            propCount += 1
                    if ifcAMCount == 1:
                        self.logger.info("Adding fake interface_ref endpoint on link %s", linkName)
                        child.appendChild(fakeiRef)
                        child.appendChild(fakeCM)
                        if propCount == 0:
                            # Add the 2 property elements
                            self.logger.debug("Adding property tags to link %s to fake node", linkName)
                            sP = self.parsedSCSRSpec.dom.createElement(Link.PROPERTY_TAG)
                            sP.setAttribute(LinkProperty.SOURCE_TAG, ifc1Name)
                            sP.setAttribute(LinkProperty.DEST_TAG, "fake:if0")
                            sP.setAttribute(LinkProperty.CAPACITY_TAG, str(self.opts.defaultCapacity))
                            dP = self.parsedSCSRSpec.dom.createElement(Link.PROPERTY_TAG)
                            dP.setAttribute(LinkProperty.DEST_TAG, ifc1Name)
                            dP.setAttribute(LinkProperty.SOURCE_TAG, "fake:if0")
                            dP.setAttribute(LinkProperty.CAPACITY_TAG, str(self.opts.defaultCapacity))
                            child.appendChild(sP)
                            child.appendChild(dP)
                        else:
                            self.logger.debug("Link %s had only interfaces at 1 am (%d of them), so added the fake interface - but it has %d properties already?", linkName, ifcCount, propCount)
                    else:
                        self.logger.debug("Not adding fake endpoint to link %s with %d interfaces at %d AMs", linkName, ifcCount, ifcAMCount)
#        self.logger.debug("\n" + self.parsedSCSRSpec.dom.toxml())