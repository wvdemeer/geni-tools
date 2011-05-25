#!/usr/bin/python

#----------------------------------------------------------------------
# Copyright (c) 2011 Raytheon BBN Technologies
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
''' Use Omni as a library to unit test API compliance'''

# FIXME: Add usage instructions
# FIXME: Each test should describe expected results

import copy as docopy
import datetime
import inspect
import math
import os
import sys
import time
import tempfile
import re
import unittest
import xml.etree.ElementTree as ET

import omni
from omni import *
from omnilib.xmlrpc.client import make_client
import omnilib.util.credparsing as credutils

SLICE_NAME='mon'
TMP_DIR = '/tmp'

################################################################################
#
# Test scripts which test AM API calls on a CH where the running user
# has permission to create slices.  This script is built on the unittest module.
#
# Purpose of the tests is to determine that AM API is functioning properly.
#
# To run all tests:
# ./api_test.py
#
# To run a single test:
# ./api_test.py Test.test_getversion
#
# To add a new test:
# Create a new method with a name starting with 'test_".  It will
# automatically be run when api_test.py is called.
# If you want the test to be part of monitoring, include a call to the
# printMonitoring method().
#
################################################################################


TEST_OPTS = None
TEST_ARGS = ()

class GENISetup(unittest.TestCase):
   def __init__(self, methodName='runTest'):
      super(GENISetup, self).__init__(methodName)
      # Add this script's args
      self.options, self.args = (TEST_OPTS, TEST_ARGS)

   def sectionBreak( self ):
      print "\n"
      print "="*80
      testname = inspect.stack()[1][3]
      preName = "NEW TEST: %s" % testname
      lenName = len(preName)
      numSpaces = int(math.floor((80-lenName)/2))
      spaceStr = " "*numSpaces
      secHeader = spaceStr+preName+spaceStr
      print secHeader
      print "-"*80

   def printMonitoring( self, result ):
      """prints a line of text like:
             MONITORING test_getversion 1"""

      if result is True:
         resultStr = 1
      else:
         resultStr = 0

      # inspect.stack()[0][3] returns the name of the method being called
      # inspect.stack()[1][3] returns the name of the parent of the method being called
      print "MONITORING %s %s" % (inspect.stack()[1][3], resultStr)      

   def create_slice_name( self ):
#      slice_name = SLICE_NAME
      slice_name = datetime.datetime.strftime(datetime.datetime.now(), SLICE_NAME+"_%H%M%S")
      return slice_name

   def call( self, cmd, options ):
      retVal= omni.call( cmd, options=options, verbose=True )
      return retVal

class Test(GENISetup):
   def test_getversion(self):
      """Passes if a call to 'getversion' on each aggregate returns
      a structure with a 'geni_api' field.
      """

      self.sectionBreak()
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      omniargs = ["getversion"]
#      print "Doing self.call %s %s" % (omniargs, options)
      (text, retDict) = self.call(omniargs, options)
      msg = "No geni_api version listed in result: \n%s" % text
      successFail = False
      if type(retDict) == type({}):
         for key,verDict in retDict.items():
            if verDict.has_key('geni_api'):
               successFail = True
               break
      self.assertTrue(successFail, msg)
      self.printMonitoring( successFail )

   def test_listresources_succ_native(self):
      """Passes if a call to 'listresources -n' on the listed
      aggregate succeeds.
      """
      self.sectionBreak()
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      omniargs = ["-n", "-a", "http://myplc.gpolab.bbn.com:12346", "listresources"]
      # Explicitly set this false so omni doesn't complain if both are true
      options.omnispec=False
#CHECK THIS 
#      print "Doing self.call %s %s" % (omniargs, options)
      (text, rspec) = self.call(omniargs, options)

      # Make sure we got an XML file back
      msg = "Returned rspec is not XML: %s" % rspec
      successFail = True
      for key, value in rspec.items():
         successFail = successFail and (ET.fromstring(value) is not None)
      self.assertTrue(successFail, msg)
      self.printMonitoring( successFail )

   def test_listresources_succ_plain(self):
      """Passes if a call to 'listresources' succeeds."""
      self.sectionBreak()
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      if options.native:
         print "Forcing use of omnispecs..."
         options.native = False

      # now construct args
      omniargs = ["listresources"]
#      print "Doing self.call %s %s" % (omniargs, options)
      (text, rspec) = self.call(omniargs, options)
      msg = "No 'resources' found in rspec: %s" % rspec
      successFail = "resources" in text
      self.assertTrue(successFail, msg)
      self.printMonitoring( successFail )

   def test_slicecreation(self):
      """Passes if the entire slice creation workflow succeeds:
      (1) createslice
      (2) renewslice (in a manner that should fail)
      (3) renewslice (in a manner that should succeed)
      (4) deleteslice
"""
      self.sectionBreak()
      successFail = True
      slice_name = self.create_slice_name()
      try:
         successFail = successFail and self.subtest_createslice( slice_name )
         successFail = successFail and self.subtest_renewslice_fail( slice_name )
         successFail = successFail and self.subtest_renewslice_success(  slice_name )
      except Exception, exp:
         print 'test_slicecreation had an error: %s' % exp
      finally:
         successFail = successFail and self.subtest_deleteslice(  slice_name )
      self.printMonitoring( successFail )

   def test_slivercreation(self):
      """Passes if the sliver creation workflow succeeds:
      (1) createslice
      (2) createsliver
      (3) sliverstatus
      (4) renewsliver (in a manner that should fail)
      (5) renewslice (to make sure the slice does not expire before the sliver expiration we are setting in the next step)
      (6) renewsliver (in a manner that should succeed)
      (7) deletesliver
      (8) deleteslice
"""
      self.sectionBreak()
      slice_name = self.create_slice_name()
      successFail = True
      try:
         successFail = successFail and self.subtest_createslice( slice_name )
         time.sleep(5)
         successFail = successFail and self.subtest_createsliver( slice_name )
         successFail = successFail and self.subtest_sliverstatus( slice_name )
         successFail = successFail and self.subtest_renewsliver_fail( slice_name )
         successFail = successFail and self.subtest_renewslice_success( slice_name )
         successFail = successFail and self.subtest_renewsliver_success( slice_name )
         successFail = successFail and self.subtest_deletesliver( slice_name )
      except Exception, exp:
         print 'test_slivercreation had an error: %s' % exp
      finally:
         try:
            self.subtest_deletesliver( slice_name )
         except:
            pass
         successFail = successFail and self.subtest_deleteslice( slice_name )

      self.printMonitoring( successFail )

   # def test_shutdown(self):
   #    self.sectionBreak()
   #    slice_name = self.create_slice_name()

   #    successFail = True
   #    try:
   #       successFail = successFail and self.subtest_createslice( slice_name )
   #       successFail = successFail and self.subtest_createsliver( slice_name )
   #       successFail = successFail and self.subtest_shutdown( slice_name )
   #       successFail = successFail and self.subtest_deletesliver( slice_name )
   #    finally:
   #       successFail = successFail and self.subtest_deleteslice( slice_name )
   #    self.printMonitoring( successFail )


   def subtest_createslice(self, slice_name ):
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      omniargs = ["createslice", slice_name]
      text, urn = self.call(omniargs, options)
      msg = "Slice creation FAILED."
      if urn is None:
         successFail = False
      else:
         successFail = True
      self.assertTrue( successFail, msg)
      return successFail

   def subtest_shutdown(self, slice_name):
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      omniargs = ["shutdown", slice_name]
      text = self.call(omniargs, options)
      msg = "Shutdown FAILED."
      successFail = ("Shutdown Sliver") in text
# FIX ME
#      self.assertTrue( successFail, msg)
#      return successFail
      return True

   def subtest_deleteslice(self, slice_name):
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      omniargs = ["deleteslice", slice_name]
      text, successFail = self.call(omniargs, options)
      msg = "Delete slice FAILED."
      # successFail = ("Delete Slice %s result:" % SLICE_NAME) in text
# FIX ME
      self.assertTrue( successFail, msg)
      return successFail

   def subtest_renewslice_success(self, slice_name):
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      newtime = (datetime.datetime.now()+datetime.timedelta(hours=12)).isoformat()
      omniargs = ["renewslice", slice_name, newtime]
      text, retTime = self.call(omniargs, options)
      msg = "Renew slice FAILED."
      if retTime is None:
         successFail = False
      else:
         successFail = True
      self.assertTrue( successFail, msg)
      return successFail

   def subtest_renewslice_fail(self, slice_name):
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      newtime = (datetime.datetime.now()+datetime.timedelta(days=-1)).isoformat()
      omniargs = ["renewslice", slice_name, newtime]
      text, retTime = self.call(omniargs, options)
      msg = "Renew slice FAILED."
      if retTime is None:
         successFail = True
      else:
         successFail = False
      self.assertTrue( successFail, msg)
      return successFail

   def subtest_renewsliver_success(self, slice_name):
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      newtime = (datetime.datetime.now()+datetime.timedelta(hours=8)).isoformat()

      omniargs = ["renewsliver", slice_name, newtime]
      text, retTime = self.call(omniargs, options)
      # if retTime is None:
      #    successFail = False
      # else:
      #    successFail = True
      m = re.search(r"Renewed slivers on (\w+) out of (\w+) aggregates", text)
      succNum = m.group(1)
      possNum = m.group(2)
      # we have reserved resources on exactly one aggregate
      successFail = (int(succNum) == 1)

      self.assertTrue( successFail )
      return successFail

   def subtest_renewsliver_fail(self, slice_name):
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      (foo, slicecred) = omni.call(["getslicecred", slice_name], options)
      sliceexp = credutils.get_cred_exp(None, slicecred)
      # try to renew the sliver for a time after the slice would expire
      # this should fail
      newtime = (sliceexp+datetime.timedelta(days=1)).isoformat()
      print "Will renew past sliceexp %s to %s" % (sliceexp, newtime)
      time.sleep(2)
      omniargs = ["renewsliver", slice_name, newtime]
      retTime = None
      try:
         text, retTime = self.call(omniargs, options)
      except:
         print "renewsliver threw exception as expected"

      msg = "Renew sliver FAILED."
      if retTime is None:
         successFail = True
      else:
         print "Renew succeeded when it should have failed? retVal: %s, retTime: %s" % (retVal, retTime)
         successFail = False
      self.assertTrue( successFail, msg )
      return successFail

   def subtest_sliverstatus(self, slice_name):
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      omniargs = ["sliverstatus", slice_name]
      text, status = self.call(omniargs, options)
      m = re.search(r"Returned status of slivers on (\w+) of (\w+) possible aggregates.", text)
      succNum = m.group(1)
      possNum = m.group(2)
      # we have reserved resources on exactly one aggregate
      successFail = (int(succNum) == 1)
      self.assertTrue( successFail )
      return successFail

   def subtest_createsliver(self, slice_name):
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      omniargs = ["-o", "listresources"]
      rspecfile = 'omnispec-1AMs.json'
      text, resourcesDict = self.call(omniargs, options)

      with open(rspecfile) as file:
         rspectext = file.readlines()
         rspectext = "".join(rspectext)
         # allocate the first resource in the rspec
         resources = re.sub('"allocate": false','"allocate": true',rspectext, 1)
      # open a temporary named file for the rspec
      filename = os.path.join( TMP_DIR, datetime.datetime.strftime(datetime.datetime.now(), "apitest_%Y%m%d%H%M%S"))
      with open(filename, mode='w') as rspec_file:
         rspec_file.write( resources )
      omniargs = ["createsliver", slice_name, rspec_file.name]
      text, result = self.call(omniargs, options)
      if result is None:
         successFail = False
      else:
         successFail = True
      # delete tmp file
      os.remove( filename )      
      self.assertTrue( successFail )
      return successFail

   def subtest_deletesliver(self, slice_name):
      options = docopy.deepcopy(self.options)
      # now modify options for this test as desired

      # now construct args
      omniargs = ["deletesliver", slice_name]
      text, successFail = self.call(omniargs, options)
      m = re.search(r"Deleted slivers on (\w+) out of a possible (\w+) aggregates", text)
      succNum = m.group(1)
      possNum = m.group(2)
      # we have reserved resources on exactly one aggregate
      successFail = (int(succNum) == 1)
      self.assertTrue( successFail )
      return successFail

   # def test_sliverstatusfail(self):
   #    self.sectionBreak()
#       options = docopy.deepcopy(self.options)
#       # now modify options for this test as desired
#
#       # now construct args
#       omniargs = ["sliverstatus", "this_slice_does_not_exist"]
   #    text = self.call(omniargs, options)
   #    print "*"*80
   #    print self.test_sliverstatusfail.__name__
   #    print "*"*80
   #    successFail = ("ERROR:omni:Call for Get Slice Cred ") in text
   #    self.assertTrue( successFail, "error message")
   #    self.printMonitoring( successFail )

if __name__ == '__main__':
   # This code uses the Omni option parser to parse the options here,
   # allowing the unit tests to take options.
   # Then we carefully edit sys.argv removing the omni options,
   # but leave the remaining options (or none) in place so that
   # the unittest optionparser doesnt throw an exception on omni
   # options, and still can get its -v or -q arguments

   import types

   # Get the omni optiosn and arguments
   parser = omni.getParser()
   TEST_OPTS, TEST_ARGS = parser.parse_args(sys.argv[1:])

   # Create a list of all omni options as they appear on commandline
   omni_options_with_arg = []
   omni_options_no_arg = []
   for opt in parser._get_all_options():
      #print "Found attr %s = %s" % (attr, getattr(TEST_OPTS, attr))
      if opt.takes_value():
         for cmdline in opt._long_opts:
            omni_options_with_arg.append(cmdline)
         for cmdline in opt._short_opts:
            omni_options_with_arg.append(cmdline)
      else:
         for cmdline in opt._long_opts:
            omni_options_no_arg.append(cmdline)
         for cmdline in opt._short_opts:
            omni_options_no_arg.append(cmdline)

   # Delete the omni options and values from the commandline
   del_lst = []
   for i,option in enumerate(sys.argv):
      if option in omni_options_with_arg:
         del_lst.append(i)
         del_lst.append(i+1)
      elif option in omni_options_no_arg:
         # Skip -v, -q, -h arguments to try to let unittest have them
         if option in ["-v", "-q", "-h"]:
            continue
         del_lst.append(i)

   del_lst.reverse()
   for i in del_lst:
      del sys.argv[i]

   # Invoke unit tests as usual
   unittest.main()

