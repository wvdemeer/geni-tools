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

from distutils.core import setup

import py2exe
import sys

setup(console=['..\src\omni.py','..\src\omni-configure.py', '..\examples/readyToLogin.py', '..\src\clear-passphrases.py'],
      name="omni",

      options={
          'py2exe':{
              'includes':'omnilib.frameworks.framework_apg, omnilib.frameworks.framework_base,\
omnilib.frameworks.framework_gcf, omnilib.frameworks.framework_gch,\
omnilib.frameworks.framework_gib, omnilib.frameworks.framework_of,\
omnilib.frameworks.framework_pg, omnilib.frameworks.framework_pgch,\
 omnilib.frameworks.framework_sfa,omnilib,sfa,dateutil,geni,\
 copy,ConfigParser,logging,optparse,os,sys,string,re,platform,shutil,zipfile,logging,subprocess',
              }
            }
        )
