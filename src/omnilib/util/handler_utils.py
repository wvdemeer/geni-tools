#----------------------------------------------------------------------
# Copyright (c) 2012 Raytheon BBN Technologies
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

import datetime
import logging
import os
import string

from dossl import _do_ssl
import credparsing as credutils
from dates import naiveUTC

def _derefAggNick(handler, aggregateNickname):
    """Check if the given aggregate string is a nickname defined
    in omni_config. If so, return the dereferenced URL,URN.
    Else return the input as the URL, and 'unspecified_AM_URN' as the URN."""

    if not aggregateNickname:
        return (None, None)
    aggregateNickname = aggregateNickname.strip()
    urn = "unspecified_AM_URN"
    url = aggregateNickname

    if handler.config['aggregate_nicknames'].has_key(aggregateNickname):
        url = handler.config['aggregate_nicknames'][aggregateNickname][1]
        tempurn = handler.config['aggregate_nicknames'][aggregateNickname][0]
        if tempurn.strip() != "":
            urn = tempurn
        handler.logger.info("Substituting AM nickname %s with URL %s, URN %s", aggregateNickname, url, urn)

    return url,urn

def _listaggregates(handler):
    """List the aggregates that can be used for the current operation.
    If an aggregate was specified on the command line, use only that one.
    Else if aggregates are specified in the config file, use that set.
    Else ask the framework for the list of aggregates.
    Returns the aggregates as a dict of urn => url pairs.
    If one URL was given on the commandline, AM URN is a constant
    If multiple URLs were given in the omni config, URN is really the URL
    """
    # used by _getclients (above), createsliver, listaggregates
    if handler.opts.aggregate:
        # Try treating that as a nickname
        # otherwise it is the url directly
        # Either way, if we have no URN, we fill in 'unspecified_AM_URN'
        url, urn = _derefAggNick(handler, handler.opts.aggregate)
        _ = urn # appease eclipse
        ret = {}
        url = url.strip()
        if url != '':
            ret[urn] = url
        return (ret, "")
    elif not handler.omni_config.get('aggregates', '').strip() == '':
        aggs = {}
        for url in handler.omni_config['aggregates'].strip().split(','):
            url = url.strip()
            if url != '':
                aggs[url] = url
        return (aggs, "")
    else:
        (aggs, message) =  _do_ssl(handler.framework, None, "List Aggregates from control framework", handler.framework.list_aggregates)
        if aggs is None:
            # FIXME: Return the message?
            return ({}, message)
        # FIXME: Check that each agg has both a urn and url key?
        return (aggs, "")

def _get_slice_cred(handler, urn):
    """Try a couple times to get the given slice credential.
    Retry on wrong pass phrase.
    Return the slice credential, and a string message of any error.
    """

    if handler.opts.slicecredfile and os.path.exists(handler.opts.slicecredfile) and os.path.isfile(handler.opts.slicecredfile) and os.path.getsize(handler.opts.slicecredfile) > 0:
        # read the slice cred from the given file
        handler.logger.info("Getting slice %s credential from file %s", urn, handler.opts.slicecredfile)
        cred = None
        with open(handler.opts.slicecredfile, 'r') as f:
            cred = f.read()
        return (cred, "")

    # Check that the return is either None or a valid slice cred
    # Callers handle None - usually by raising an error
    (cred, message) = _do_ssl(handler.framework, None, "Get Slice Cred for slice %s" % urn, handler.framework.get_slice_cred, urn)
    if cred is not None and (not (type(cred) is str and cred.startswith("<"))):
        #elif slice_cred is not XML that looks like a credential, assume
        # assume it's an error message, and raise an omni_error
        handler.logger.error("Got invalid slice credential for slice %s: %s" % (urn, cred))
        cred = None
        message = "Invalid slice credential returned"
    return (cred, message)

def _print_slice_expiration(handler, urn, sliceCred=None):
    """Check when the slice expires. Print varying warning notices
    and the expiration date"""
    # FIXME: push this to config?
    shorthours = 3
    middays = 1

# This could be used to print user credential expiration info too...

    if sliceCred is None:
        if urn is None or urn == '':
            return ""
        (sliceCred, _) = _get_slice_cred(handler, urn)
    if sliceCred is None:
        # failed to get a slice string. Can't check
        return ""

    sliceexp = credutils.get_cred_exp(handler.logger, sliceCred)
    sliceexp = naiveUTC(sliceexp)
    now = datetime.datetime.utcnow()
    if sliceexp <= now:
        retVal = 'Slice %s has expired at %s UTC' % (urn, sliceexp)
        handler.logger.warn('Slice %s has expired at %s UTC' % (urn, sliceexp))
    elif sliceexp - datetime.timedelta(hours=shorthours) <= now:
        retVal = 'Slice %s expires in <= %d hours on %s UTC' % (urn, shorthours, sliceexp)
        handler.logger.warn('Slice %s expires in <= %d hours' % (urn, shorthours))
        handler.logger.info('Slice %s expires on %s UTC' % (urn, sliceexp))
        handler.logger.debug('It is now %s UTC' % (datetime.datetime.utcnow()))
    elif sliceexp - datetime.timedelta(days=middays) <= now:
        retVal = 'Slice %s expires within %d day(s) on %s UTC' % (urn, middays, sliceexp)
        handler.logger.info('Slice %s expires within %d day on %s UTC' % (urn, middays, sliceexp))
    else:
        retVal = 'Slice %s expires on %s UTC' % (urn, sliceexp)
        handler.logger.info('Slice %s expires on %s UTC' % (urn, sliceexp))
    return retVal

def validate_url(url):
    """Basic sanity checks on URLS before trying to use them.
    Return None on success, error string if there is a problem.
    If return starts with WARN: then just log a warning - not fatal."""

    import urlparse
    pieces = urlparse.urlparse(url)
    if not all([pieces.scheme, pieces.netloc]):
        return "Invalid URL: %s" % url
    if not pieces.scheme in ["http", "https"]:
        return "Invalid URL. URL should be http or https protocol: %s" % url
    if not set(pieces.netloc) <= set(string.letters+string.digits+'-.:'):
        return "Invalid URL. Host/port has invalid characters in url %s" % url

    # Look for common errors in contructing the urls

    # FIXME: check cache to find common URL typos?

# GCF Ticket #66: This check is just causing confusion. And will be OBE with FOAM.
#    # if the urn part of the urn is openflow/gapi (no trailing slash)
#    # then warn it needs a trailing slash for Expedient
#    if pieces.path.lower().find('/openflow/gapi') == 0 and pieces.path != '/openflow/gapi/':
#        return "WARN: Likely invalid Expedient URL %s. Expedient AM runs at /openflow/gapi/ - try url https://%s/openflow/gapi/" % (url, pieces.netloc)

# GCF ticket #66: Not sure these checks are helping either.
# Right thing may be to test the URL and see if an AM is running there, rather
# than this approach.

#    # If the url has no path part but a port that is 123?? and not 12346
#    # then warn and suggest SFA AMs typically run on 12346
#    if (pieces.path is None or pieces.path.strip() == "" or pieces.path.strip() == '/') and pieces.port >= 12300 and pieces.port < 12400 and pieces.port != 12346:
#        return "WARN: Likely invalid SFA URL %s. SFA AM typically runs on port 12346. Try AM URL https://%s:12346/" % (url, pieces.hostname)

#    # if the non host part has 'protogeni' and is not protogeni/xmlrpc/am
#    # then warn that PG AM interface is at protogeni/xmlrpc/am
#    if pieces.path.lower().find('/protogeni') == 0 and pieces.path != '/protogeni/xmlrpc/am' and pieces.path != '/protogeni/xmlrpc/am/':
#        return "WARN: Likely invalid PG URL %s: PG AMs typically run at /protogeni/xmlrpc/am - try url https://%s/protogeni/xmlrpc/am" % (url, pieces.netloc)

    return None
