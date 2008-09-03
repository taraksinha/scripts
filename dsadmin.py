import sys
import os
import os.path
import popen2
import base64
import urllib
import urllib2
import socket
import ldif
import re
import ldap
import cStringIO
import time
import operator
import tempfile

from ldap.ldapobject import SimpleLDAPObject

REPLBINDDN = ''
REPLBINDCN = ''
REPLBINDPW = ''
REPLICAID = 1 # the initial replica ID

(MASTER_TYPE,
 HUB_TYPE,
 LEAF_TYPE) = range(3)

class Error(Exception): pass
class InvalidArgumentError(Error):
    def __init__(self,message): self.message = message
    def __repr__(self): return message
class NoSuchEntryError(Error):
    def __init__(self,message): self.message = message
    def __repr__(self): return message

class Entry:
    """This class represents an LDAP Entry object.  An LDAP entry consists of a DN
    and a list of attributes.  Each attribute consists of a name and a list of
    values.  In python-ldap, entries are returned as a list of 2-tuples.
    Instance variables:
    dn - string - the string DN of the entry
    data - cidict - case insensitive dict of the attributes and values"""

    def __init__(self,entrydata):
        """data is the raw data returned from the python-ldap result method, which is
        a search result entry or a reference or None.
        If creating a new empty entry, data is the string DN."""
        if entrydata:
            if isinstance(entrydata,tuple):
                self.dn = entrydata[0]
                self.data = ldap.cidict.cidict(entrydata[1])
            elif isinstance(entrydata,str) or isinstance(entrydata,unicode):
                self.dn = entrydata
                self.data = ldap.cidict.cidict()
        else:
            self.dn = ''
            self.data = ldap.cidict.cidict()

    def __nonzero__(self):
        """This allows us to do tests like if entry: returns false if there is no data,
        true otherwise"""
        return self.data != None and len(self.data) > 0

    def hasAttr(self,name):
        """Return True if this entry has an attribute named name, False otherwise"""
        return self.data and self.data.has_key(name)

    def __getattr__(self,name):
        """If name is the name of an LDAP attribute, return the first value for that
        attribute - equivalent to getValue - this allows the use of
        entry.cn
        instead of
        entry.getValue('cn')
        This also allows us to return None if an attribute is not found rather than
        throwing an exception"""
        return self.getValue(name)

    def getValues(self,name):
        """Get the list (array) of values for the attribute named name"""
        return self.data.get(name)

    def getValue(self,name):
        """Get the first value for the attribute named name"""
        return self.data.get(name,[None])[0]

    def hasValue(self,name,val):
        """True if the given attribute is present and has the given value"""
        if not self.hasAttr(name): return False
        return val in self.data.get(name)

    def hasValueCase(self,name,val):
        """True if the given attribute is present and has the given value - case insensitive value match"""
        if not self.hasAttr(name): return False
        return val.lower() in [x.lower() for x in self.data.get(name)]

    def setValue(self,name,*value):
        """Value passed in may be a single value, several values, or a single sequence.
        For example:
           ent.setValue('name', 'value')
           ent.setValue('name', 'value1', 'value2', ..., 'valueN')
           ent.setValue('name', ['value1', 'value2', ..., 'valueN'])
           ent.setValue('name', ('value1', 'value2', ..., 'valueN'))
        Since *value is a tuple, we may have to extract a list or tuple from that
        tuple as in the last two examples above"""
        if isinstance(value[0],list) or isinstance(value[0],tuple):
            self.data[name] = value[0]
        else:
            self.data[name] = value

    setValues = setValue

    def toTupleList(self):
        """Convert the attrs and values to a list of 2-tuples.  The first element
        of the tuple is the attribute name.  The second element is either a
        single value or a list of values."""
        return self.data.items()

    def __str__(self):
        """Convert the Entry to its LDIF representation"""
        return self.__repr__()

    # the ldif class base64 encodes some attrs which I would rather see in raw form - to
    # encode specific attrs as base64, add them to the list below
    ldif.safe_string_re = re.compile('^$')
    base64_attrs = ['nsstate']

    def __repr__(self):
        """Convert the Entry to its LDIF representation"""
        sio = cStringIO.StringIO()
        # what's all this then?  the unparse method will currently only accept
        # a list or a dict, not a class derived from them.  self.data is a
        # cidict, so unparse barfs on it.  I've filed a bug against python-ldap,
        # but in the meantime, we have to convert to a plain old dict for printing
        # I also don't want to see wrapping, so set the line width really high (1000)
        newdata = {}
        newdata.update(self.data)
        ldif.LDIFWriter(sio,Entry.base64_attrs,1000).unparse(self.dn,newdata)
        return sio.getvalue()

def wrapper(f,name):
    """This is the method that wraps all of the methods of the superclass.  This seems
    to need to be an unbound method, that's why it's outside of DSAdmin.  Perhaps there
    is some way to do this with the new classmethod or staticmethod of 2.4.
    Basically, we replace every call to a method in SimpleLDAPObject (the superclass
    of DSAdmin) with a call to inner.  The f argument to wrapper is the bound method
    of DSAdmin (which is inherited from the superclass).  Bound means that it will implicitly
    be called with the self argument, it is not in the args list.  name is the name of
    the method to call.  If name is a method that returns entry objects (e.g. result),
    we wrap the data returned by an Entry class.  If name is a method that takes an entry
    argument, we extract the raw data from the entry object to pass in."""
    def inner(*args, **kargs):
        if name == 'result':
            type, data = f(*args, **kargs)
            # data is either a 2-tuple or a list of 2-tuples
            # print data
            if data:
                if isinstance(data,tuple):
                    return type, Entry(data)
                elif isinstance(data,list):
                    return type, [Entry(x) for x in data]
                else:
                    raise TypeError, "unknown data type %s returned by result" % type(data)
            else:
                return type, data
        elif name.startswith('add'):
            # the first arg is self
            # the second and third arg are the dn and the data to send
            # We need to convert the Entry into the format used by
            # python-ldap
            ent = args[0]
            if isinstance(ent,Entry):
                return f(ent.dn, ent.toTupleList(), *args[2:])
            else:
                return f(*args, **kargs)
        else:
            return f(*args, **kargs)
    return inner

class LDIFConn(ldif.LDIFParser):
    def __init__(
        self,
        input_file,
        ignored_attr_types=None,max_entries=0,process_url_schemes=None
    ):
        """
        See LDIFParser.__init__()
        
        Additional Parameters:
        all_records
        List instance for storing parsed records
        """
        self.dndict = {} # maps dn to Entry
        self.dnlist = [] # contains entries in order read
        myfile = input_file
        if isinstance(input_file,str) or isinstance(input_file,unicode):
            myfile = open(input_file, "r")
        ldif.LDIFParser.__init__(self,myfile,ignored_attr_types,max_entries,process_url_schemes)
        self.parse()
        if isinstance(input_file,str) or isinstance(input_file,unicode):
            myfile.close()

    def handle(self,dn,entry):
        """
        Append single record to dictionary of all records.
        """
        if not dn:
            dn = ''
        newentry = Entry((dn, entry))
        self.dndict[DSAdmin.normalizeDN(dn)] = newentry
        self.dnlist.append(newentry)

    def get(self,dn):
        ndn = DSAdmin.normalizeDN(dn)
        return self.dndict.get(ndn, Entry(None))

class DSAdmin(SimpleLDAPObject):
    CFGSUFFIX = "o=NetscapeRoot"
    DEFAULT_USER_ID = "nobody"

    def getDseAttr(self,attrname):
        conffile = self.confdir + '/dse.ldif'
        try:
            dseldif = LDIFConn(conffile)
            cnconfig = dseldif.get("cn=config")
            if cnconfig:
                return cnconfig.getValue(attrname)
        except IOError, err:
            print "could not read dse config file", err
        return None
    
    def __initPart2(self):
        if self.binddn and len(self.binddn) and not hasattr(self,'sroot'):
            try:
                ent = self.getEntry('cn=config', ldap.SCOPE_BASE, '(objectclass=*)',
                                    [ 'nsslapd-instancedir', 'nsslapd-errorlog',
                                      'nsslapd-certdir', 'nsslapd-schemadir' ])
                self.errlog = ent.getValue('nsslapd-errorlog')
                self.confdir = ent.getValue('nsslapd-certdir')
                if self.isLocal:
                    if not self.confdir or not os.access(self.confdir + '/dse.ldif', os.R_OK):
                        self.confdir = ent.getValue('nsslapd-schemadir')
                        if self.confdir:
                            self.confdir = os.path.dirname(self.confdir)
                instdir = ent.getValue('nsslapd-instancedir')
                if not instdir and self.isLocal:
                    # get instance name from errorlog
                    self.inst = re.match(r'(.*)[\/]slapd-(\w+)/errors', self.errlog).group(2)
                    if self.isLocal and self.confdir:
                        instdir = self.getDseAttr('nsslapd-instancedir')
                    else:
                        instdir = re.match(r'(.*/slapd-.*)/logs/errors', self.errlog).group(1)
                if not instdir:
                    instdir = self.confdir
                self.sroot, self.inst = re.match(r'(.*)[\/]slapd-(\w+)$', instdir).groups()
                ent = self.getEntry('cn=config,cn=ldbm database,cn=plugins,cn=config',
                                    ldap.SCOPE_BASE, '(objectclass=*)',
                                    [ 'nsslapd-directory' ])
                self.dbdir = os.path.dirname(ent.getValue('nsslapd-directory'))
            except (ldap.INSUFFICIENT_ACCESS, ldap.CONNECT_ERROR, NoSuchEntryError):
                pass # usually means 
#                print "ignored exception"
            except ldap.OPERATIONS_ERROR, e:
                print "caught exception ", e
                print "Probably Active Directory, pass"
                pass # usually means this is Active Directory
            except ldap.LDAPError, e:
                print "caught exception ", e
                raise

    def __localinit__(self):
        SimpleLDAPObject.__init__(self,'ldap://%s:%d' % (self.host,self.port))
        # see if binddn is a dn or a uid that we need to lookup
        if self.binddn and not DSAdmin.is_a_dn(self.binddn):
            self.simple_bind_s("","") # anon
            ent = self.getEntry(DSAdmin.CFGSUFFIX, ldap.SCOPE_SUBTREE,
                                "(uid=%s)" % self.binddn,
                                ['uid'])
            if ent:
                self.binddn = ent.dn
            else:
                print "Error: could not find %s under %s" % (self.binddn, DSAdmin.CFGSUFFIX)
        self.simple_bind_s(self.binddn,self.bindpw)
        self.__initPart2()
                
    def __init__(self,host,port,binddn='',bindpw=''): # default to anon bind
        """We just set our instance variables and wrap the methods - the real work is
        done in __localinit__ and __initPart2 - these are separated out this way so
        that we can call them from places other than instance creation e.g. when
        using the start command, we just need to reconnect, not create a new instance"""
        self.__wrapmethods()
        self.port = port or 389
        self.sslport = 0
        self.host = host
        self.binddn = binddn
        self.bindpw = bindpw
        self.isLocal = DSAdmin.isLocalHost(host)
        self.suffixes = {}
        self.__localinit__()

    def __str__(self):
        return self.host + ":" + str(self.port)

    def toLDAPURL(self):
        return "ldap://%s:%d/" % (self.host,self.port)

    def getEntry(self,*args):
        """This wraps the search function.  It is common to just get one entry"""
        res = self.search(*args)
        type, obj = self.result(res)
        if not obj:
            raise NoSuchEntryError("no such entry for " + str(args) + "\n")
        elif isinstance(obj,Entry):
            return obj
        else: # assume list/tuple
            return obj[0]

    def __wrapmethods(self):
        """This wraps all methods of SimpleLDAPObject, so that we can intercept
        the methods that deal with entries.  Instead of using a raw list of tuples
        of lists of hashes of arrays as the entry object, we want to wrap entries
        in an Entry class that provides some useful methods"""
        for name in dir(self.__class__.__bases__[0]):
            attr = getattr(self, name)
            if callable(attr):
                setattr(self, name, wrapper(attr, name))

    def serverCmd(self,cmd,verbose,timeout):
        instanceDir = self.sroot + "/slapd-" + self.inst
        errLog = instanceDir + '/logs/errors'
        if hasattr(self, 'errlog'):
            errLog = self.errlog
        done = False
        started = True
        code = 0
        lastLine = ""
        cmd = cmd.lower()
        fullCmd = instanceDir + "/" + cmd + "-slapd"
        if cmd == 'start':
            cmdPat = 'slapd started.'
        else:
            cmdPat = 'slapd stopped.'

        timeout = timeout or 120 # default is 120 seconds
        timeout = int(time.time()) + timeout
        if cmd == 'stop':
            self.unbind()
        logfp = open(errLog, 'r')
        logfp.seek(0, 2) # seek to end
        pos = logfp.tell() # get current position
        logfp.seek(pos, 0) # reset the EOF flag
        rc = os.system(fullCmd)
        while not done and int(time.time()) < timeout:
            line = logfp.readline().strip()
            while not done and line:
                lastLine = line
                if verbose: print line
                if line.find(cmdPat) >= 0:
                    started += 1
                    if started == 2: done = True
                elif line.find("Initialization Failed") >= 0:
                    # sometimes the server fails to start - try again
                    rc = os.system(fullCmd)
                elif line.find("exiting.") >= 0:
                    # possible transient condition - try again
                    rc = os.system(fullCmd)
                pos = logfp.tell()
                line = logfp.readline().strip()
            if line.find("PR_Bind") >= 0:
                # server port conflicts with another one, just report and punt
                print lastLine
                print "This server cannot be started until the other server on this"
                print "port is shutdown"
                done = True
            if not done:
                time.sleep(2)
                logfp.seek(pos, 0)
        logfp.close()
        if started < 2:
            now = int(time.time())
            if now > timeout:
                print "Probable timeout: timeout=%d now=%d" % (timeout, now)
            if verbose:
                print "Error: could not %s server %s %s: %d" % (cmd, self.sroot, self.inst, rc)
            return 1
        else:
            if verbose:
                print "%s was successful for %s %s" % (cmd, self.sroot, self.inst)
            if cmd == 'start':
                self.__localinit__()
        return 0

    def stop(self,verbose=False,timeout=0):
        if not self.isLocal and hasattr(self, 'asport'):
            if verbose:
                print "stopping remote server ", self
            self.unbind()
            if verbose:
                print "closed remote server ", self
            cgiargs = {}
            rc = DSAdmin.cgiPost(self.host, self.asport, self.cfgdsuser,
                                 self.cfgdspwd,
                                 "/slapd-%s/Tasks/Operation/stop" % self.inst,
                                 verbose, cgiargs)
            if verbose:
                print "stopped remote server %s rc = %d" % (self, rc)
            return rc
        else:
            return self.serverCmd('stop', verbose, timeout)

    def start(self,verbose=False,timeout=0):
        if not self.isLocal and hasattr(self, 'asport'):
            if verbose:
                print "starting remote server ", self
            cgiargs = {}
            rc = DSAdmin.cgiPost(self.host, self.asport, self.cfgdsuser,
                                 self.cfgdspwd,
                                 "/slapd-%s/Tasks/Operation/start" % self.inst,
                                 verbose, cgiargs)
            if verbose:
                print "connecting remote server", self
            if not rc:
                self.__localinit__()
            if verbose:
                print "started remote server %s rc = %d" % (self, rc)
            return rc
        else:
            return self.serverCmd('start', verbose, timeout)

    def startTaskAndWait(self,entry,verbose=False):
        # start the task
        dn = entry.dn
        self.add_s(entry)
        entry = self.getEntry(dn, ldap.SCOPE_BASE)
        if not entry:
            if verbose:
                print "Entry %s was added successfully, but I cannot search it" % dn
                return -1
        elif verbose:
            print entry

        # wait for task completion - task is complete when the nsTaskExitCode attr is set
        attrlist = ['nsTaskLog', 'nsTaskStatus', 'nsTaskExitCode', 'nsTaskCurrentItem', 'nsTaskTotalItems']
        done = False
        exitCode = 0
        while not done:
            time.sleep(1)
            entry = self.getEntry(dn, ldap.SCOPE_BASE, "(objectclass=*)", attrlist)
            if verbose:
                print entry
            if entry.nsTaskExitCode:
                exitCode = int(entry.nsTaskExitCode)
                done = True
        return exitCode

    def importLDIF(self,file,suffix,be=None,verbose=False):
        cn = "import" + str(int(time.time()));
        dn = "cn=%s, cn=import, cn=tasks, cn=config" % cn
        entry = Entry(dn)
        entry.setValues('objectclass', 'top', 'extensibleObject')
        entry.setValues('cn', cn)
        entry.setValues('nsFilename', file)
        if be:
            entry.setValues('nsInstance', be)
        else:
            entry.setValues('nsIncludeSuffix', suffix)

        rc = self.startTaskAndWait(entry, verbose)

        if rc:
            if verbose:
                print "Error: import task %s for file %s exited with %d" % (cn,file,rc)
        else:
            if verbose:
                print "Import task %s for file %s completed successfully" % (cn,file)
        return rc

    def exportLDIF(self, file, suffix, forrepl=False, verbose=False):
        cn = "export" + str(int(time.time()))
        dn = "cn=%s, cn=export, cn=tasks, cn=config" % cn
        entry = Entry(dn)
        entry.setValues('objectclass', 'top', 'extensibleObject')
        entry.setValues('cn', cn)
        entry.setValues('nsFilename', file)
        entry.setValues('nsIncludeSuffix', suffix)
        if forrepl:
            entry.setValues('nsExportReplica', 'true')

        rc = self.startTaskAndWait(entry, verbose)

        if rc:
            if verbose:
                print "Error: export task %s for file %s exited with %d" % (cn,file,rc)
        else:
            if verbose:
                print "Export task %s for file %s completed successfully" % (cn,file)
        return rc

    def setupBackend(self,suffix,binddn=None,bindpw=None,urls=[],attrvals={}):
        ldbmdn = "cn=ldbm database, cn=plugins, cn=config"
        chaindn = "cn=chaining database, cn=plugins, cn=config"
        dnbase = ""
        benamebase = ""
        # figure out what type of be based on args
        if binddn and bindpw and urls: # its a chaining be
            benamebase = "chaindb"
            dnbase = chaindn
        else: # its a ldbm be
            benamebase = "localdb"
            dnbase = ldbmdn

        nsuffix = DSAdmin.normalizeDN(suffix)
        benum = 1
        done = False
        while not done:
            try:
                cn = benamebase + str(benum) # e.g. localdb1
                dn = "cn=" + cn + ", " + dnbase
                entry = Entry(dn)
                entry.setValues('objectclass', 'top', 'extensibleObject', 'nsBackendInstance')
                entry.setValues('cn', cn)
                entry.setValues('nsslapd-suffix', nsuffix)
                if binddn and bindpw and urls: # its a chaining be
                    entry.setValues('nsfarmserverurl', urls)
                    entry.setValues('nsmultiplexorbinddn', binddn)
                    entry.setValues('nsmultiplexorcredentials', bindpw)
                else: # set ldbm parameters, if any
                    pass
                    #	  $entry->add('nsslapd-cachesize' => '-1');
                    #	  $entry->add('nsslapd-cachememsize' => '2097152');
                if attrvals:
                    for attr,val in attrvals.items():
                        print "adding %s = %s to entry %s" % (attr,val,dn)
                        entry.setValues(attr, val)
                print entry
                self.add_s(entry)
                done = True
            except ldap.ALREADY_EXISTS:
                benum += 1
            except ldap.LDAPError, e:
                print "Could not add backend entry " + dn, e
                raise
        entry = self.getEntry(dn, ldap.SCOPE_BASE)
        if not entry:
            print "Backend entry added, but could not be searched"
        else:
            print entry
            return cn

        return ""

    def setupSuffix(self,suffix,bename,parent=""):
        rc = 0
        nsuffix = DSAdmin.normalizeDN(suffix)
        nparent = ""
        if parent: nparent = DSAdmin.normalizeDN(parent)
        dn = "cn=\"%s\", cn=mapping tree, cn=config" % nsuffix
        try:
            entry = self.getEntry("cn=mapping tree, cn=config", ldap.SCOPE_SUBTREE,
                                  "(|(cn=\"%s\")(cn=\"%s\"))" % (suffix,nsuffix))
        except NoSuchEntryError: entry = None
        if not entry:
            dn = "cn=\"%s\", cn=mapping tree, cn=config" % nsuffix
            entry = Entry(dn)
            entry.setValues('objectclass', 'top', 'extensibleObject', 'nsMappingTree')
            entry.setValues('cn', "\"%s\"" % nsuffix)
            entry.setValues('nsslapd-state', 'backend')
            entry.setValues('nsslapd-backend', bename)
            if parent: entry.setValues('nsslapd-parent-suffix', "\"%s\"" % nparent)
            try:
                self.add_s(entry)
                entry = self.getEntry(dn, ldap.SCOPE_BASE)
                if not entry:
                    print "Entry %s was added successfully, but I cannot search it" % dn
                    rc = -1
                else:
                    print entry
            except LDAPError, e:
                print "Error adding suffix entry " + dn, e
                raise
        else:
            print "Suffix entry already exists:"
            print entry

        return rc

    def getMTEntry(self, suffix, attrs=[]):
        """Given a suffix, return the mapping tree entry for it.  If attrs is
        given, only fetch those attributes, otherwise, get all attributes."""
        nsuffix = DSAdmin.normalizeDN(suffix)
        try:
            entry = self.getEntry("cn=mapping tree,cn=config", ldap.SCOPE_ONELEVEL,
                                  "(|(cn=\"%s\")(cn=\"%s\"))" % (suffix,nsuffix),
                                  attrs)
        except NoSuchEntryError: pass
        return entry        

    def getBackendsForSuffix(self,suffix, attrs=[]):
        nsuffix = DSAdmin.normalizeDN(suffix)
        try:
            entries = self.search_s("cn=plugins,cn=config", ldap.SCOPE_SUBTREE,
                                    "(&(objectclass=nsBackendInstance)(|(nsslapd-suffix=%s)(nsslapd-suffix=%s)))" % (suffix,nsuffix),
                                    attrs)
        except NoSuchEntryError: pass
        return entries

    # given a backend name, return the mapping tree entry for it
    def getSuffixForBackend(self, bename, attrs=[]):
        try:
            entry = self.getEntry("cn=plugins,cn=config", ldap.SCOPE_SUBTREE,
                                  "(&(objectclass=nsBackendInstance)(cn=%s))" % bename,
                                  ['nsslapd-suffix'])
        except NoSuchEntryError:
            print "Could not find and entry for backend", bename
        if entry:
            suffix = entry.getValue('nsslapd-suffix')
            return self.getMTEntry(suffix, attrs)
        return None

    # see if the given suffix has a parent suffix
    def findParentSuffix(self, suffix):
        rdns = ldap.explode_dn(suffix)
        nsuffix = DSAdmin.normalizeDN(suffix)
        nrdns = ldap.explode_dn(nsuffix)
        del rdns[0]
        del nrdns[0]
        if len(rdns) == 0:
            return ""

        while len(rdns) > 0:
            suffix = ','.join(rdns)
            nsuffix = ','.join(nrdns)
            mapent = None
            try:
                mapent = self.getEntry('cn=mapping tree, cn=config', ldap.SCOPE_SUBTREE,
                                       "(|(cn=\"%s\")(cn=\"%s\"))" % (suffix,nsuffix),
                                       ['cn'])
            except NoSuchEntryError: pass
            if mapent:
                return suffix
            else:
                del rdns[0]
                del nrdns[0]

        return ""

    def addSuffix(self, suffix, binddn=None, bindpw=None, urls=[]):
        beents = self.getBackendsForSuffix(suffix, ['cn'])
        bename = ""
        benames = []
        # no backends for this suffix yet - create one
        if not beents:
            bename = self.setupBackend(suffix,binddn,bindpw,urls)
            if not bename:
                print "Couldn't create backend for", suffix
                return -1 # ldap error code handled already
        else: # use existing backend(s)
            benames = [entry.cn for entry in beents]
            bename = benames.pop(0)

        parent = self.findParentSuffix(suffix)
        if self.setupSuffix(suffix, bename, parent):
            print "Couldn't create suffix for %s %s" % (bename, suffix)
            return -1
        
        return 0

    def waitForEntry(self, dn, timeout=7200, attr='', quiet=False):
        scope = ldap.SCOPE_BASE
        filter = "(objectclass=*)"
        attrlist = []
        if attr:
            filter = "(%s=*)" % attr
            attrlist.append(attr)
        timeout += int(time.time())

        if isinstance(dn,Entry):
            dn = dn.dn

        # wait for entry and/or attr to show up
        if not quiet:
            sys.stdout.write("Waiting for %s %s:%s " % (self,dn,attr))
            sys.stdout.flush()
        entry = None
        while not entry and int(time.time()) < timeout:
            try:
                entry = self.getEntry(dn, scope, filter, attrlist)
            except NoSuchEntryError: pass # found entry, but no attr
            except ldap.NO_SUCH_OBJECT: pass # no entry yet
            except ldap.LDAPError, e: # badness
                print "\nError reading entry", dn, e
                break
            if not entry:
                if not quiet:
                    sys.stdout.write(".")
                    sys.stdout.flush()
                time.sleep(1)

        if not entry and int(time.time()) > timeout:
            print "\nwaitForEntry timeout for %s for %s" % (self,dn)
        elif entry and not quiet:
            print "\nThe waited for entry is:", entry
        else:
            print "\nError: could not read entry %s from %s" % (dn,self)

        return entry

    # specify the suffix (should contain 1 local database backend),
    # the name of the attribute to index, and the types of indexes
    # to create e.g. "pres", "eq", "sub"
    def addIndex(self, suffix, attr, indexTypes, *matchingRules):
        beents = self.getBackendsForSuffix(suffix, ['cn'])
        # assume 1 local backend
        dn = "cn=%s,cn=index,%s" % (attr, beents[0].dn)
        entry = Entry(dn)
        entry.setValues('objectclass', 'top', 'nsIndex')
        entry.setValues('cn', attr)
        entry.setValues('nsSystemIndex', "false")
        entry.setValues('nsIndexType', indexTypes)
        if matchingRules:
            entry.setValues('nsMatchingRule', matchingRules)
        try:
            self.add_s(entry)
        except ldap.ALREADY_EXISTS:
            print "Index for attr %s for backend %s already exists" % (attr, dn)

    def requireIndex(self, suffix):
        beents = self.getBackendsForSuffix(suffix, ['cn'])
        # assume 1 local backend
        dn = beents[0].dn
        replace = [(ldap.MOD_REPLACE, 'nsslapd-require-index', 'on')]
        self.modify_s(dn, replace)

    def addSchema(self, attr, val):
        dn = "cn=schema"
        self.modify_s(dn, [(ldap.MOD_ADD, attr, val)])

    def addAttr(self, *args):
        return self.addSchema('attributeTypes', args)

    def addObjClass(self, *args):
        return self.addSchema('objectClasses', args)

    def enableReplLogging(self):
        return self.setLogLevel(8192)

    def disableReplLogging(self):
        return self.setLogLevel(0)

    def setLogLevel(self, *vals):
        val = reduce(operator.add, vals)
        self.modify_s('cn=config', [(ldap.MOD_REPLACE, 'nsslapd-errorlog-level', str(val))])

    def setAccessLogLevel(self, *vals):
        val = reduce(operator.add, vals)
        self.modify_s('cn=config', [(ldap.MOD_REPLACE, 'nsslapd-accesslog-level', str(val))])

    def setupChainingIntermediate(self):
        confdn = "cn=config,cn=chaining database,cn=plugins,cn=config"
        try:
            self.modify_s(confdn, [(ldap.MOD_ADD, 'nsTransmittedControl',
                                   [ '2.16.840.1.113730.3.4.12', '1.3.6.1.4.1.1466.29539.12' ])])
        except ldap.TYPE_OR_VALUE_EXISTS:
            print "chaining backend config already has the required controls"

    def setupChainingMux(self, suffix, isIntermediate, binddn, bindpw, urls):
        self.addSuffix(suffix, binddn, bindpw, urls)
        if isIntermediate:
            self.setupChainingIntermediate()

    def setupChainingFarm(self, suffix, binddn, bindcn, bindpw):
        # step 1 - create the bind dn to use as the proxy
        self.setupBindDN(binddn, bindcn, bindpw)
        self.addSuffix(suffix) # step 2 - create the suffix
        # step 3 - add the proxy ACI to the suffix
        try:
            self.modify_s(suffix, [(ldap.MOD_ADD, 'aci',
                                    [ "(targetattr = \"*\")(version 3.0; acl \"Proxied authorization for database links\"; allow (proxy) userdn = \"ldap:///%s\";)" % binddn ])])
        except ldap.TYPE_OR_VALUE_EXISTS:
            print "proxy aci already exists in suffix %s for %s" % (suffix, binddn)

    # setup chaining from self to to - self is the mux, to is the farm
    # if isIntermediate is set, this server will chain requests from another server to to
    def setupChaining(self, to, suffix, isIntermediate):
        bindcn = "chaining user"
        binddn = "cn=%s,cn=config" % bindcn
        bindpw = "chaining"

        to.setupChainingFarm(suffix, binddn, bindcn, bindpw)
        self.setupChainingMux(suffix, isIntermediate, binddn, bindpw, to.toLDAPURL());

    def setupChangelog(self, dirpath=''):
        dn = "cn=changelog5, cn=config"
        dirpath = dirpath or self.dbdir + "/cldb"
        entry = Entry(dn)
        entry.setValues('objectclass', "top", "extensibleobject")
        entry.setValues('cn', "changelog5")
        entry.setValues('nsslapd-changelogdir', dirpath)
        try:
            self.add_s(entry)
        except ldap.ALREADY_EXISTS:
            print "entry %s already exists" % dn
            return 0

        entry = self.getEntry(dn, ldap.SCOPE_BASE)
        if not entry:
            print "Entry %s was added successfully, but I cannot search it" % dn
            return -1
        else:
            print entry
        return 0

    def enableChainOnUpdate(self, suffix, bename):
        # first, get the mapping tree entry to modify
        mtent = self.getMTEntry(suffix, ['cn'])
        dn = mtent.dn

        # next, get the path of the replication plugin
        plgent = self.getEntry("cn=Multimaster Replication Plugin,cn=plugins,cn=config",
                               ldap.SCOPE_BASE, "(objectclass=*)", ['nsslapd-pluginPath'])
        path = plgent.getValue('nsslapd-pluginPath')

        mod = [(ldap.MOD_REPLACE, 'nsslapd-state', 'backend'),
               (ldap.MOD_ADD, 'nsslapd-backend', bename),
               (ldap.MOD_ADD, 'nsslapd-distribution-plugin', path),
               (ldap.MOD_ADD, 'nsslapd-distribution-funct', 'repl_chain_on_update')]

        try:
            self.modify_s(dn, mod)
        except ldap.TYPE_OR_VALUE_EXISTS:
            print "chainOnUpdate already enabled for %s" % suffix

    def setupConsumerChainOnUpdate(self, suffix, isIntermediate, binddn, bindpw, urls):
        # suffix should already exist
        # we need to create a chaining backend
        chainbe = self.setupBackend(suffix, binddn, bindpw, urls,
                                    {'nsCheckLocalACI': 'on'}) # enable local db aci eval.
        # do the stuff for intermediate chains
        if isIntermediate:
            self.setupChainingIntermediate()
        # enable the chain on update
        return self.enableChainOnUpdate(suffix, chainbe)

    # arguments to set up a replica:
    # suffix - dn of suffix
    # binddn - the replication bind dn for this replica
    # type - master, hub, leaf (see above for values) - if type is omitted, default is master
    # legacy - true or false - for legacy consumer
    # id - replica id
    # if replica ID is not given, an internal sequence number will be assigned
    # call like this:
    # conn.setupReplica({
    #        'suffix': "dc=mcom, dc=com",
    #        'type'  : dsadmin.MASTER_TYPE,
    #        'binddn': "cn=replication manager, cn=config"
    #  })
    # binddn can also be a list:
    #    'binddn': [ "cn=repl1, cn=config", "cn=repl2, cn=config" ]
    def setupReplica(self, args):
        global REPLICAID # declared here because we may assign to it
        suffix = args['suffix']
        type = args.get('type', MASTER_TYPE)
        legacy = args.get('legacy', False)
        binddn = args['binddn']
        id = args.get('id', None)
        nsuffix = DSAdmin.normalizeDN(suffix)
        dn = "cn=replica, cn=\"%s\", cn=mapping tree, cn=config" % nsuffix
        try:
            entry = self.getEntry(dn, ldap.SCOPE_BASE)
        except ldap.NO_SUCH_OBJECT: entry = None
        if entry:
            print "Already setup replica for suffix", suffix
            self.suffixes[nsuffix] = {}
            self.suffixes[nsuffix]['dn'] = dn
            self.suffixes[nsuffix]['type'] = type
            return 0

        binddnlist = []
        if binddn:
            if isinstance(binddn, str):
                binddnlist.append(binddn)
            else:
                binddnlist = binddn
        else:
            binddnlist.append(REPLBINDDN)

        if not id and type == MASTER_TYPE:
            id = REPLICAID
            REPLICAID += 1
        elif not id:
            id = 0
        else:
            REPLICAID = id # use given id for internal counter
            
        if type == MASTER_TYPE:
            replicatype = "3"
        else:
            replicatype = "2"
            
        if legacy:
            legacyval = "on"
        else:
            legacyval = "off"

        entry = Entry(dn)
        entry.setValues('objectclass', "top", "nsds5replica", "extensibleobject")
        entry.setValues('cn', "replica")
        entry.setValues('nsds5replicaroot', nsuffix)
        entry.setValues('nsds5replicaid', str(id))
        entry.setValues('nsds5replicatype', replicatype)
        if type != LEAF_TYPE:
            entry.setValues('nsds5flags', "1")
        entry.setValues('nsds5replicabinddn', binddnlist)
        entry.setValues('nsds5replicalegacyconsumer', legacyval)
        if args.has_key('tpi'):
            entry.setValues('nsds5replicatombstonepurgeinterval', args['tpi'])
        if args.has_key('pd'):
            entry.setValues('nsds5ReplicaPurgeDelay', args['pd'])
        if args.has_key('referrals'):
            entry.setValues('nsds5ReplicaReferral', args['referrals'])
        if args.has_key('fractional'):
            entry.setValues('nsDS5ReplicatedAttributeList', args['fractional'])
        self.add_s(entry)
        entry = self.getEntry(dn, ldap.SCOPE_BASE)
        if not entry:
            print "Entry %s was added successfully, but I cannot search it" % dn
            return -1
        else:
            print entry
        self.suffixes[nsuffix] = {}
        self.suffixes[nsuffix]['dn'] = dn
        self.suffixes[nsuffix]['type'] = type
        return 0

    # dn can be an entry
    def setupBindDN(self, dn, cn, pwd):
        if dn and isinstance(dn,Entry):
            dn = dn.dn
        elif not dn:
            dn = REPLBINDDN

        cn = cn or REPLBINDCN
        pwd = pwd or REPLBINDPW

        ent = Entry(dn)
        ent.setValues('objectclass', "top", "person")
        ent.setValues('cn', cn)
        ent.setValues('userpassword', pwd)
        ent.setValues('sn', "bind dn pseudo user")
        try:
            self.add_s(ent)
        except ldap.ALREADY_EXISTS:
            print "Entry %s already exists" % dn
        ent = self.getEntry(dn, ldap.SCOPE_BASE)
        if not ent:
            print "Entry %s was added successfully, but I cannot search it" % dn
            return -1
        else:
            print ent
        return 0

    def setupReplBindDN(self, dn, cn, pwd):
        return self.setupBindDN(dn, cn, pwd)

    def setupWinSyncAgmt(self, repoth, args, entry):
        if not args.has_key('winsync'):
            return

        suffix = args['suffix']
        entry.setValues("objectclass", "nsDSWindowsReplicationAgreement")
        entry.setValues("nsds7WindowsReplicaSubtree",
                        args.get("win_subtree",
                                 "cn=users," + suffix))
        entry.setValues("nsds7DirectoryReplicaSubtree",
                        args.get("ds_subtree",
                                 "ou=People," + suffix))
        entry.setValues("nsds7NewWinUserSyncEnabled", args.get('newwinusers', 'true'))
        entry.setValues("nsds7NewWinGroupSyncEnabled", args.get('newwingroups', 'true'))
        windomain = ''
        if args.has_key('windomain'):
            windomain = args['windomain']
        else:
            windomain = '.'.join(ldap.explode_dn(suffix, 1))
        entry.setValues("nsds7WindowsDomain", windomain)

    # args - DSAdmin consumer (repoth), suffix, binddn, bindpw, timeout
    # also need an auto_init argument
    def setupAgreement(self, repoth, args):
        """Create a replication agreement from self to repoth - that is, self is the
        supplier and repoth is the DSAdmin object for the consumer (the consumer
        can be a master) """
        suffix = args['suffix']
        nsuffix = DSAdmin.normalizeDN(suffix)
        othhost, othport, othsslport = (repoth.host, repoth.port, repoth.sslport)
        othport = othsslport or othport
        cn = "meTo%s%d" % (othhost,othport)
        dn = "cn=%s, %s" % (cn,self.suffixes[nsuffix]['dn'])
        try:
            entry = self.getEntry(dn, ldap.SCOPE_BASE)
        except ldap.NO_SUCH_OBJECT: entry = None
        if entry:
            print "Agreement exists:"
            print entry
            self.suffixes[nsuffix] = {}
            self.suffixes[nsuffix][str(repoth)] = dn
            return dn

        entry = Entry(dn)
        binddn = args.get('binddn', REPLBINDDN)
        bindpw = args.get('bindpw', REPLBINDPW)
        entry.setValues('objectclass', "top", "nsds5replicationagreement")
        entry.setValues('cn', cn)
        entry.setValues('nsds5replicahost', othhost)
        entry.setValues('nsds5replicaport', str(othport))
        entry.setValues('nsds5replicatimeout', str(args.get('timeout', 120)))
        entry.setValues('nsds5replicabinddn', binddn)
        entry.setValues('nsds5replicacredentials', bindpw)
        entry.setValues('nsds5replicabindmethod', 'simple')
        entry.setValues('nsds5replicaroot', nsuffix)
        entry.setValues('nsds5replicaupdateschedule', '0000-2359 0123456')
        entry.setValues('description', "me to %s%d" % (othhost,othport));
        if othsslport:
            entry.setValues('nsds5replicatransportinfo', 'SSL')
        if args.has_key('fractional'):
            entry.setValues('nsDS5ReplicatedAttributeList', args['fractional'])
        if args.has_key('auto_init'):
            entry.setValues('nsds5BeginReplicaRefresh', 'start')

        self.setupWinSyncAgmt(repoth, args, entry)
        try:
            self.add_s(entry)
        except:
            raise
        entry = self.waitForEntry(dn)
        if entry:
            self.suffixes[nsuffix][str(repoth)] = dn
            chain = args.has_key('chain')
            if chain and self.suffixes[nsuffix]['type'] == MASTER_TYPE:
                self.setupChainingFarm(suffix, binddn, '', bindpw)
            if chain and repoth.suffixes[nsuffix]['type'] == LEAF_TYPE:
                repoth.setupConsumerChainOnUpdate(suffix, 0, binddn, bindpw, self.toLDAPURL())
            elif chain and repoth.suffixes[nsuffix]['type'] == HUB_TYPE:
                repoth.setupConsumerChainOnUpdate(suffix, 1, binddn, bindpw, self.toLDAPURL())
        return dn

    def stopReplication(self, agmtdn):
        mod = [(ldap.MOD_REPLACE, 'nsds5replicaupdateschedule', [ '2358-2359 0' ])]
        self.modify_s(agmtdn, mod)

    def restartReplication(self,agmtdn):
        mod = [(ldap.MOD_REPLACE, 'nsds5replicaupdateschedule', [ '0000-2359 0123456' ])]
        self.modify_s(agmtdn, mod)

    def findAgreementDNs(self,filt='',attrs=[]):
        realfilt = "(objectclass=nsds5ReplicationAgreement)"
        if filt:
            realfilt = "(&%s%s)" % (realfilt,filt)
        if not attrs:
            attrs.append('cn')
        ents = self.search_s("cn=mapping tree,cn=config", ldap.SCOPE_SUBTREE, realfilt, attrs)
        return [ent.dn for ent in ents]

    def getReplStatus(self,agmtdn):
        attrlist = ['cn', 'nsds5BeginReplicaRefresh', 'nsds5replicaUpdateInProgress',
					'nsds5ReplicaLastInitStatus', 'nsds5ReplicaLastInitStart',
				    'nsds5ReplicaLastInitEnd', 'nsds5replicaReapActive',
				    'nsds5replicaLastUpdateStart', 'nsds5replicaLastUpdateEnd',
				    'nsds5replicaChangesSentSinceStartup', 'nsds5replicaLastUpdateStatus',
				    'nsds5replicaChangesSkippedSinceStartup', 'nsds5ReplicaHost',
				    'nsds5ReplicaPort']
        ent = self.getEntry(agmtdn, ldap.SCOPE_BASE, "(objectclass=*)", attrlist)
        if not ent:
            print "Error reading status from agreement", agmtdn
        else:
            rh = ent.nsds5ReplicaHost
            rp = ent.nsds5ReplicaPort
            retstr = "Status for %s agmt %s:%s:%s" % (self,ent.cn,rh,rp)
            retstr += "\tUpdate In Progress  : " + ent.nsds5replicaUpdateInProgress + "\n"
            retstr += "\tLast Update Start   : " + ent.nsds5replicaLastUpdateStart + "\n"
            retstr += "\tLast Update End     : " + ent.nsds5replicaLastUpdateEnd + "\n"
            retstr += "\tNum. Changes Sent   : " + ent.nsds5replicaChangesSentSinceStartup + "\n"
            retstr += "\tNum. Changes Skipped: " + str(ent.nsds5replicaChangesSkippedSinceStartup) + "\n"
            retstr += "\tLast Update Status  : " + ent.nsds5replicaLastUpdateStatus + "\n"
            retstr += "\tInit in Progress    : " + str(ent.nsds5BeginReplicaRefresh) + "\n"
            retstr += "\tLast Init Start     : " + ent.nsds5ReplicaLastInitStart + "\n"
            retstr += "\tLast Init End       : " + ent.nsds5ReplicaLastInitEnd + "\n"
            retstr += "\tLast Init Status    : " + str(ent.nsds5ReplicaLastInitStatus) + "\n"
            retstr += "\tReap In Progress    : " + ent.nsds5replicaReapActive + "\n"
            return retstr

        return ""

    def startReplication_async(self, agmtdn):
        mod = [(ldap.MOD_ADD, 'nsds5BeginReplicaRefresh', 'start')]
        self.modify_s(agmtdn, mod)

    # returns tuple - first element is done/not done, 2nd is no error/has error
    def checkReplInit(self, agmtdn):
        done = False
        hasError = 0
        attrlist = ['cn', 'nsds5BeginReplicaRefresh', 'nsds5replicaUpdateInProgress',
					'nsds5ReplicaLastInitStatus', 'nsds5ReplicaLastInitStart',
				    'nsds5ReplicaLastInitEnd']
        entry = self.getEntry(agmtdn, ldap.SCOPE_BASE, "(objectclass=*)", attrlist)
        if not entry:
            print "Error reading status from agreement", agmtdn
            hasError = 1
        else:
            refresh = entry.nsds5BeginReplicaRefresh
            inprogress = entry.nsds5replicaUpdateInProgress
            status = entry.nsds5ReplicaLastInitStatus
            start = entry.nsds5ReplicaLastInitStart
            end = entry.nsds5ReplicaLastInitEnd
            if not refresh: # done - check status
                if not status:
                    print "No status yet"
                elif status.find("replica busy") > -1:
                    print "Update failed - replica busy - status", status
                    done = True
                    hasError = 2
                elif status.find("Total update succeeded") > -1:
                    print "Update succeeded: status ", status
                    done = True
                elif inprogress.lower() == 'true':
                    print "Update in progress yet not in progress: status ", status
                else:
                    print "Update failed: status", status
                    hasError = 1
                    done = True
            else:
                print "Update in progress: status", status

        return done, hasError

    def waitForReplInit(self,agmtdn):
        done = False
        haserror = 0
        while not done and not haserror:
            time.sleep(1)  # give it a few seconds to get going
            done, haserror = self.checkReplInit(agmtdn)
        return haserror

    def startReplication(self, agmtdn):
        rc = self.startReplication_async(agmtdn)
        if not rc:
            rc = self.waitForReplInit(agmtdn)
            if rc == 2: # replica busy - retry
                rc = self.startReplication(agmtdn)
        return rc

    # setup everything needed to enable replication for a given suffix
    # argument - a dict with the following fields
    #	suffix - suffix to set up for replication
    # optional fields and their default values
    #	bename - name of backend corresponding to suffix
    #	parent - parent suffix if suffix is a sub-suffix - default is undef
    #	ro - put database in read only mode - default is read write
    #	type - replica type (MASTER_TYPE, HUB_TYPE, LEAF_TYPE) - default is master
    #	legacy - make this replica a legacy consumer - default is no
    #	binddn - bind DN of the replication manager user - default is REPLBINDDN
    #	bindcn - bind CN of the replication manager user - default is REPLBINDCN
    #	bindpw - bind password of the repl manager - default is REPLBINDPW
    #	log - if true, replication logging is turned on - default false
    #	id - the replica ID - default is an auto incremented number
    def replicaSetupAll(self, repArgs):
        repArgs.setdefault('type', MASTER_TYPE)
        self.addSuffix(repArgs['suffix'])
        if not repArgs.has_key('bename'):
            beents = self.getBackendsForSuffix(repArgs['suffix'], ['cn'])
            # just use first one
            repArgs['bename'] = beents[0].cn
        if repArgs.has_key('log'):
            self.enableReplLogging()
        if repArgs['type'] != LEAF_TYPE:
            self.setupChangelog()
        self.setupReplBindDN(repArgs.get('binddn'), repArgs.get('bindcn'), repArgs.get('bindpw'))
        self.setupReplica(repArgs)
        if repArgs.has_key('legacy'):
            self.setupLegacyConsumer(repArgs.get('binddn'), repArgs.get('bindpw'))

    def setPwdPolicy(self, pwdpolicy, **pwdargs):
        """input is dict of attr/vals"""
        dn = "cn=config"
        mods = []
        for (attr, val) in pwdpolicy.iteritems():
            mods.append((ldap.MOD_REPLACE, attr, str(val)))
        if pwdargs:
            for (attr, val) in pwdargs.iteritems():
                mods.append((ldap.MOD_REPLACE, attr, str(val)))
        self.modify_s(dn, mods)

    ###########################
    # Static methods start here
    ###########################
    def normalizeDN(dn):
        # not great, but will do until we use a newer version of python-ldap
        # that has DN utilities
        ary = ldap.explode_dn(dn.lower())
        return ",".join(ary)
    normalizeDN = staticmethod(normalizeDN)

    def isLocalHost(hname):
        # first see if this is a "well known" local hostname
        if hname == 'localhost' or hname == 'localhost.localdomain':
            return True

        # first lookup ip addr
        ipadr = None
        try:
            ipadr = socket.gethostbyname(hname)
        except:
            pass
        if not ipadr:
            print "Error: no IP Address for", hname
            return False

        # next, see if this IP addr is one of our
        # local addresses
        thematch = re.compile('inet addr:' + ipadr)
        found = False
        (cout, cerr) = popen2.popen2('/sbin/ifconfig -a')
        for line in cout:
            if re.search(thematch, line):
                found = True
                break

        cout.close()
        cerr.close()
        return found
    isLocalHost = staticmethod(isLocalHost)

    def getfqdn(name=''):
        return socket.getfqdn(name)
    getfqdn = staticmethod(getfqdn)

    def getdomainname(name=''):
        fqdn = DSAdmin.getfqdn(name)
        index = fqdn.find('.')
        if index >= 0:
            return fqdn[index+1:]
        else:
            return fqdn
    getdomainname = staticmethod(getdomainname)

    def getdefaultsuffix(name=''):
        dm = DSAdmin.getdomainname(name)
        if dm:
            return "dc=" + dm.replace('.', ', dc=')
        else:
            return 'dc=localdomain'
    getdefaultsuffix = staticmethod(getdefaultsuffix)

    def getnewhost(args):
        """One of the arguments to createInstance is newhost.  If this is specified, we need
        to convert it to the fqdn.  If not given, we need to figure out what the fqdn of the
        local host is.  This method sets newhost in args to the appropriate value and
        returns True if newhost is the localhost, False otherwise"""
        isLocal = False
        if args.has_key('newhost'):
            args['newhost'] = DSAdmin.getfqdn(args['newhost'])
            isLocal = DSAdmin.isLocalHost(args['newhost'])
        else:
            isLocal = True
            args['newhost'] = DSAdmin.getfqdn()
        return isLocal
    getnewhost = staticmethod(getnewhost)

    def getoldcfgdsinfo(args):
        """Use the old style sroot/shared/config/dbswitch.conf to get the info"""
        dbswitch = open("%s/shared/config/dbswitch.conf" % args['sroot'], 'r')
        try:
            matcher = re.compile(r'^directory\s+default\s+')
            for line in dbswitch:
                m = matcher.match(line)
                if m:
                    url = LDAPUrl(line[m.end():])
                    ary = url.hostport.split(":")
                    if len(ary) < 2:
                        ary.append('389')
                    ary.append(url.dn)
                    return ary
        finally:
            dbswitch.close()
    getoldcfgdsinfo = staticmethod(getoldcfgdsinfo)

    def getnewcfgdsinfo(args):
        """Use the new style prefix/etc/dirsrv/admin-serv/adm.conf"""
        url = LDAPUrl(args['admconf'].ldapurl)
        ary = url.hostport.split(":")
        if len(ary) < 2:
            ary.append('389')
        ary.append(url.dn)
        return ary
    getnewcfgdsinfo = staticmethod(getnewcfgdsinfo)

    def getcfgdsinfo(args):
        """We need the host and port of the configuration directory server in order
        to create an instance.  If this was not given, read the dbswitch.conf file
        to get the information.  This method will raise an exception if the file
        was not found or could not be open.  This assumes args contains the sroot
        parameter for the server root path.  If successful, returns a 3-tuple
        consisting of the host, port, and cfg suffix."""
        if not args.has_key('cfgdshost') or not args.has_key('cfgdsport'):
            if args['new_style']:
                return getnewcfgdsinfo(args)
            else:
                return getoldcfgdsinfo(args)
        else:
            return args['cfgdshost'], args['cfgdsport'], DSAdmin.CFGSUFFIX
    getcfgdsinfo = staticmethod(getcfgdsinfo)

    def is_a_dn(dn):
        """Returns True if the given string is a DN, False otherwise."""
        return (dn.find("=") > 0)
    is_a_dn = staticmethod(is_a_dn)

    def getcfgdsuserdn(cfgdn,args):
        """If the config ds user ID was given, not the full DN, we need to figure
        out what the full DN is.  Try to search the directory anonymously first.  If
        that doesn't work, look in ldap.conf.  If that doesn't work, just try the
        default DN.  This may raise a file or LDAP exception.  Returns a DSAdmin
        object bound as either anonymous or the admin user."""
        # create a connection to the cfg ds
        conn = DSAdmin(args['cfgdshost'], args['cfgdsport'], "", "")
        # if the caller gave a password, but not the cfguser DN, look it up
        if args.has_key('cfgdspwd') and \
               (not args.has_key('cfgdsuser') or not DSAdmin.is_a_dn(args['cfgdsuser'])):
            if args.has_key('cfgdsuser'):
                ent = conn.getEntry(cfgdn, ldap.SCOPE_SUBTREE,
                                    "(uid=%s)" % args['cfgdsuser'],
                                    [ 'dn' ])
                args['cfgdsuser'] = ent.dn
            elif args.has_key('sroot'):
                ldapconf = open("%s/shared/config/ldap.conf" % args['sroot'], 'r')
                for line in ldapconf:
                    ary = line.split() # default split is all whitespace
                    if len(ary) > 1 and ary[0] == 'admnm':
                        args['cfgdsuser'] = ary[-1]
                ldapconf.close()
            elif args.has_key('admconf'):
                args['cfgdsuser'] = args['admconf'].userdn
            elif args.has_key('cfgdsuser'):
                args['cfgdsuser'] = "uid=%s, ou=Administrators, ou=TopologyManagement, %s" % \
                	(args['cfgdsuser'], cfgdn)
            conn.unbind()
            conn = DSAdmin(args['cfgdshost'], args['cfgdsport'], args['cfgdsuser'],
                           args['cfgdspwd'])
        return conn
    getcfgdsuserdn = staticmethod(getcfgdsuserdn)

    def getserverroot(cfgconn, isLocal, args):
        """Grab the serverroot from the instance dir of the config ds if the user
        did not specify a server root directory"""
        if cfgconn and not args.has_key('sroot') and isLocal:
            ent = cfgconn.getEntry("cn=config", ldap.SCOPE_BASE, "(objectclass=*)",
                                   [ 'nsslapd-instancedir' ])
            if ent:
                args['sroot'] = os.path.dirname(ent.getValue('nsslapd-instancedir'))
    getserverroot = staticmethod(getserverroot)

    def getadmindomain(isLocal,args):
        """Get the admin domain to use."""
        if isLocal and not args.has_key('admin_domain'):
            if args.has_key('admconf'):
                args['admin_domain'] = args['admconf'].admindomain
            elif args.has_key('sroot'):
                dsconf = open('%s/shared/config/ds.conf' % args['sroot'], 'r')
                for line in dsconf:
                    ary = line.split(":")
                    if len(ary) > 1 and ary[0] == 'AdminDomain':
                        args['admin_domain'] = ary[1].strip()
                dsconf.close()
    getadmindomain = staticmethod(getadmindomain)

    def getadminport(cfgconn,cfgdn,args):
        """Get the admin server port so we can contact it via http.  We get this from
        the configuration entry using the CFGSUFFIX and cfgconn.  Also get any other
        information we may need from that entry.  The return value is a 2-tuple consisting
        of the asport and True if the admin server is using SSL, False otherwise."""
        asport = 0
        secure = False
        if cfgconn:
            dn = cfgdn
            if args.has_key('admin_domain'):
                dn = "cn=%s, ou=%s, %s" % (args['newhost'], args['admin_domain'], cfgdn)
            filter = "(&(objectclass=nsAdminServer)(serverHostName=%s)" % args['newhost']
            if args.has_key('sroot'):
                filter += "(serverRoot=%s)" % args['sroot']
            filter += ")"
            ent = cfgconn.getEntry(dn, ldap.SCOPE_SUBTREE, filter, ['serverRoot'])
            if ent:
                if not args.has_key('sroot') and ent.serverRoot:
                    args['sroot'] = ent.serverRoot
                if not args.has_key('admin_domain'):
                    ary = ldap.explode_dn(ent.dn,1)
                    args['admin_domain'] = ary[-2]
                dn = "cn=configuration, " + ent.dn
                ent = cfgconn.getEntry(dn, ldap.SCOPE_BASE, '(objectclass=*)',
                                       ['nsServerPort', 'nsSuiteSpotUser', 'nsServerSecurity'])
                if ent:
                    asport = ent.nsServerPort
                    secure = (ent.nsServerSecurity and (ent.nsServerSecurity == 'on'))
                    if not args.has_key('newuserid'):
                        args['newuserid'] = ent.nsSuiteSpotUser
            cfgconn.unbind()
        return asport, secure
    getadminport = staticmethod(getadminport)

    def getserveruid(args):
        if not args.has_key('newuserid'):
            if args.has_key('admconf'):
                args['newuserid'] = args['admconf'].SuiteSpotUserID
            elif args.has_key('sroot'):
                ssusers = open("%s/shared/config/ssusers.conf" % args['sroot'])
                for line in ssusers:
                    ary = line.split()
                    if len(ary) > 1 and ary[0] == 'SuiteSpotUser':
                        args['newuserid'] = ary[-1]
                ssusers.close()
        if not args.has_key('newuserid'):
            args['newuserid'] = os.environ['LOGNAME']
            if args['newuserid'] == 'root':
                args['newuserid'] = DEFAULT_USER_ID
    getserveruid = staticmethod(getserveruid)

    def cgiFake(sroot,verbose,prog,args):
        """Run the local program prog as a CGI using the POST method."""
        content = urllib.urlencode(args)
        length = len(content)
        # setup CGI environment
        os.environ['REQUEST_METHOD'] = "POST"
        os.environ['NETSITE_ROOT'] = sroot
        os.environ['CONTENT_LENGTH'] = str(length)
        curdir = os.getcwd()
        progdir = os.path.dirname(prog)
        exe = os.path.basename(prog)
        os.chdir(progdir)
        try:
            pipe = popen2.Popen4("./" + exe)
            pipe.tochild.write(content)
            pipe.tochild.close()
            for line in pipe.fromchild:
                if verbose: print line
                ary = line.split(":")
                if len(ary) > 1 and ary[0] == 'NMC_Status':
                    exitCode = ary[1].strip()
                    break
            pipe.fromchild.close()
            osCode = pipe.wait()
        finally:
            os.chdir(curdir)
        print "%s returned NMC code %s and OS code %s" % (prog, exitCode, osCode)
        return exitCode
    cgiFake = staticmethod(cgiFake)

    def formatInfData(args):
        """Format args data for input to setup or migrate taking inf style data"""
        content = """[General]
FullMachineName= %s
SuiteSpotUserID= %s
""" % (args['newhost'], args['newuserid'])

        if args['have_admin']:
            content = content + """
ConfigDirectoryLdapURL= ldap://%s:%s/%s
ConfigDirectoryAdminID= %s
ConfigDirectoryAdminPwd= %s
AdminDomain= %s
""" % (args['cfgdshost'], args['cfgdsport'],
       DSAdmin.CFGSUFFIX,
       args['cfgdsuser'], args['cfgdspwd'], args['admin_domain'])

        content = content + """

[slapd]
ServerPort= %s
RootDN= %s
RootDNPwd= %s
ServerIdentifier= %s
Suffix= %s
""" % (args['newport'], args['newrootdn'], args['newrootpw'],
       args['newinst'], args['newsuffix'])

        if args.has_key('ConfigFile'):
            for ff in args['ConfigFile']:
                content = content + """
ConfigFile= %s
""" % ff
        if args.has_key('SchemaFile'):
            for ff in args['SchemaFile']:
                content = content + """
SchemaFile= %s
""" % ff

        return content
    formatInfData = staticmethod(formatInfData)

    def runInfProg(prog,content,verbose):
        """run a program that takes an .inf style file on stdin"""
        ntf = tempfile.NamedTemporaryFile()
        ntf.file.write(content)
        ntf.file.close()
        cmd = prog
        if verbose:
            cmd += ' -ddd'
        cmd += ' -s -f ' + ntf.name
        exitCode = os.system(cmd)
        ntf.unlink(ntf.name)
#         pipe = popen2.Popen4(prog + ' -s -f -')
#         pipe.tochild.write(content)
#         pipe.tochild.close()
#         for line in pipe.fromchild:
#             if verbose: sys.stdout.write(line)
#         pipe.fromchild.close()
#         exitCode = pipe.wait()
        print "%s returned exit code %s" % (prog, exitCode)
        return exitCode        
    runInfProg = staticmethod(runInfProg)

    def cgiPost(host, port, username, password, uri, verbose, secure, args):
        """Post the request to the admin server.  Admin server requires authentication,
        so we use the auth handler classes.  NOTE: the url classes in python use the
        deprecated base64.encodestring() function, which truncates lines, causing Apache
        to give us a 400 Bad Request error for the Authentication string.  So, we have
        to tell base64.encodestring() not to truncate."""
        prefix = 'http'
        if secure: prefix = 'https'
        hostport = host + ":" + port
        # construct our url
        url = '%s://%s:%s%s' % (prefix,host,port,uri)
        # tell base64 not to truncate lines
        savedbinsize = base64.MAXBINSIZE
        base64.MAXBINSIZE = 256
        # create the password manager - we don't care about the realm
        passman = urllib2.HTTPPasswordMgrWithDefaultRealm()
        # add our password
        passman.add_password(None, hostport, username, password)
        # create the auth handler
        authhandler = urllib2.HTTPBasicAuthHandler(passman)
        # create our url opener that handles basic auth
        opener = urllib2.build_opener(authhandler)
        # make admin server think we are the console
        opener.addheaders = [('User-Agent', 'Fedora-Console/1.0')]
        if verbose:
            print "requesting url", url
            sys.stdout.flush()
        exitCode = 1
        try:
            req = opener.open(url, urllib.urlencode(args))
            for line in req:
                if verbose: print line
                ary = line.split(":")
                if len(ary) > 1 and ary[0] == 'NMC_Status':
                    exitCode = ary[1].strip()
                    break
            req.close()
#         except IOError, e:
#             print e
#             print e.code
#             print e.headers
#             raise
        finally:
            # restore binsize
            base64.MAXBINSIZE = savedbinsize
        return exitCode            
    cgiPost = staticmethod(cgiPost)

    def getsbindir(sroot,prefix):
        if sroot:
            return "%s/bin/slapd/admin/bin" % sroot
        elif prefix:
            return "%s/sbin" % prefix
        return "/usr/sbin"
    getsbindir = staticmethod(getsbindir)

    def createInstance(args):
        """Create a new instance of directory server.  First, determine the hostname to use.  By
        default, the server will be created on the localhost.  Also figure out if the given
        hostname is the local host or not."""
        verbose = args.get('verbose', 0)
        isLocal = DSAdmin.getnewhost(args)

        # old style or new style?
        sroot = args.get('sroot', os.environ.get('SERVER_ROOT', None))
        if sroot and not args.has_key('sroot'):
            args['sroot'] = sroot
        # new style - prefix or FHS?
        prefix = args.get('prefix', os.environ.get('PREFIX', None))
        if not args.has_key('prefix'):
            args['prefix'] = (prefix or '')
        args['new_style'] = not sroot

        # do we have ds only or ds+admin?
        if not args.has_key('no_admin'):
            sbindir = DSAdmin.getsbindir(sroot,prefix)
            if os.path.isfile(sbindir + '/setup-ds-admin.pl'):
                args['have_admin'] = True

        if not args.has_key('have_admin'):
            args['have_admin'] = False

        # get default values from adm.conf
        if args['new_style'] and args['have_admin']:
            admconf = LDIFConn(args['prefix'] + "/etc/dirsrv/admin-serv/adm.conf")
            args['admconf'] = admconf.get('')

        # next, get the configuration ds host and port
        if args['have_admin']:
            args['cfgdshost'], args['cfgdsport'], cfgdn = DSAdmin.getcfgdsinfo(args)
        if args['have_admin']:
            cfgconn = DSAdmin.getcfgdsuserdn(cfgdn,args)
        # next, get the server root if not given
        if not args['new_style']:
            DSAdmin.getserverroot(cfgconn,isLocal,args)
        # next, get the admin domain
        if args['have_admin']:
            DSAdmin.getadmindomain(isLocal,args)
        # next, get the admin server port and any other information - close the cfgconn
        if args['have_admin']:
            asport, secure = DSAdmin.getadminport(cfgconn,cfgdn,args)
        # next, get the server user id
        DSAdmin.getserveruid(args)
        # fixup and verify other args
        if not args.has_key('newport'): args['newport'] = '389'
        if not args.has_key('newrootdn'): args['newrootdn'] = 'cn=directory manager'
        if not args.has_key('newsuffix'): args['newsuffix'] = DSAdmin.getdefaultsuffix(args['newhost'])
        if not isLocal or args.has_key('cfgdshost'):
            if not args.has_key('admin_domain'): args['admin_domain'] = DSAdmin.getdomainname(args['newhost'])
            if isLocal and not args.has_key('cfgdspwd'): args['cfgdspwd'] = "dummy"
            if isLocal and not args.has_key('cfgdshost'): args['cfgdshost'] = args['newhost']
            if isLocal and not args.has_key('cfgdsport'): args['cfgdsport'] = '55555'
        missing = False
        for param in ('newhost', 'newport', 'newrootdn', 'newrootpw', 'newinst', 'newsuffix'):
            if not args.has_key(param):
                print "missing required argument", param
                missing = True
        if not isLocal or args.has_key('cfgdshost'):
            for param in ('cfgdshost', 'cfgdsport', 'cfgdsuser', 'cfgdspwd', 'admin_domain'):
                if not args.has_key(param):
                    print "missing required argument", param
                    missing = True
        if not isLocal and not asport:
            print "missing required argument admin server port"
            missing = True
        if missing:
            raise InvalidArgumentError("missing required arguments")

        # try to connect with the given parameters
        try:
            newconn = DSAdmin(args['newhost'], args['newport'], args['newrootdn'], args['newrootpw'])
            newconn.isLocal = isLocal
            if args['have_admin']:
                newconn.asport = asport
                newconn.cfgdsuser = args['cfgdsuser']
                newconn.cfgdspwd = args['cfgdspwd']
            print "Warning: server at %s:%s already exists, returning connection to it" % \
                  (args['newhost'], args['newport'])
            return newconn
        except ldap.SERVER_DOWN:
            pass # not running - create new one

        # construct a hash table with our CGI arguments - used with cgiPost
        # and cgiFake
        cgiargs = {
            'servname'    : args['newhost'],
            'servport'    : args['newport'],
            'rootdn'      : args['newrootdn'],
            'rootpw'      : args['newrootpw'],
            'servid'      : args['newinst'],
            'suffix'      : args['newsuffix'],
            'servuser'    : args['newuserid'],
            'start_server': 1
        }
        if args.has_key('cfgdshost'):
            cgiargs['cfg_sspt_uid'] = args['cfgdsuser']
            cgiargs['cfg_sspt_uid_pw'] = args['cfgdspwd']
            cgiargs['ldap_url'] = "ldap://%s:%s/%s" % (args['cfgdshost'], args['cfgdsport'], cfgdn)
            cgiargs['admin_domain'] = args['admin_domain']
    
        if not isLocal:
            DSAdmin.cgiPost(args['newhost'], asport, args['cfgdsuser'],
                            args['cfgdspwd'], "/slapd/Tasks/Operation/Create", verbose,
                            secure, cgiargs)
        elif not args['new_style']:
            prog = args['sroot'] + "/bin/slapd/admin/bin/ds_create"
            if not os.access(prog,os.X_OK):
                prog = args['sroot'] + "/bin/slapd/admin/bin/ds_newinst"
            DSAdmin.cgiFake(args['sroot'], verbose, prog, cgiargs)
        else:
            prog = ''
            if args['have_admin']:
                prog = DSAdmin.getsbindir(sroot,prefix) + "/setup-ds-admin.pl"
            else:
                prog = DSAdmin.getsbindir(sroot,prefix) + "/setup-ds.pl"
            content = DSAdmin.formatInfData(args)
            DSAdmin.runInfProg(prog, content, verbose)

        newconn = DSAdmin(args['newhost'], args['newport'],
                          args['newrootdn'], args['newrootpw'])
        newconn.isLocal = isLocal
        if args['have_admin']:
            newconn.asport = asport
            newconn.cfgdsuser = args['cfgdsuser']
            newconn.cfgdspwd = args['cfgdspwd']
        return newconn
    createInstance = staticmethod(createInstance)

    # pass this sub two dicts - the first one is a dict suitable to create
    # a new instance - see createInstance for more details
    # the second is a dict suitable for replicaSetupAll - see replicaSetupAll
    def createAndSetupReplica(createArgs, repArgs):
        conn = DSAdmin.createInstance(createArgs)
        if not conn:
            print "Error: could not create server", createArgs
            return 0

        conn.replicaSetupAll(repArgs)
        return conn
    createAndSetupReplica = staticmethod(createAndSetupReplica)

def testit():
    host = 'localhost'
    port = 10200
    binddn = "cn=directory manager"
    bindpw = "secret12"

    basedn = "cn=config"
    scope = ldap.SCOPE_BASE
    filter = "(objectclass=*)"

    try:
        m1 = DSAdmin(host,port,binddn,bindpw)
#        filename = "%s/slapd-%s/ldif/Example.ldif" % (m1.sroot, m1.inst)
#        m1.importLDIF(filename, "dc=example,dc=com", None, True)
#        m1.exportLDIF('/tmp/ldif', "dc=example,dc=com", False, True)
        print m1.sroot, m1.inst, m1.errlog
        ent = m1.getEntry(basedn, scope, filter, None)
        if ent:
            print ent.passwordmaxage
        m1 = DSAdmin.createInstance({
         'cfgdshost': host,
         'cfgdsport': port,
         'cfgdsuser': 'admin',
         'cfgdspwd' : 'admin',
         'newrootpw': 'password',
         'newhost'  : host,
         'newport'  : port+10,
         'newinst'  : 'm1',
         'newsuffix': 'dc=example,dc=com',
         'verbose': 1
         })
#     m1.stop(True)
#     m1.start(True)
        cn = m1.setupBackend("dc=example2,dc=com")
        rc = m1.setupSuffix("dc=example2,dc=com", cn)
        entry = m1.getEntry("cn=config", ldap.SCOPE_SUBTREE, "(cn=" + cn + ")")
        print "new backend entry is:"
        print entry
        print entry.getValues('objectclass')
        print entry.OBJECTCLASS
        results = m1.search_s("cn=monitor", ldap.SCOPE_SUBTREE)
        print results
        results = m1.getBackendsForSuffix("dc=example,dc=com")
        print results

    except ldap.LDAPError, e:
        print e

    print "done"
    
if __name__ == "__main__":
    testit()