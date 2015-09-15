# Copyright (C) 2010-2015 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import sys
import time
import socket
import logging
from lib.core.config import Config
from threading import Lock
from ptrace.syscall import SYSCALL_NAMES, SOCKET_SYSCALL_NAMES

log = logging.getLogger()

try:
    import bson
    HAVE_BSON = True
except ImportError:
    HAVE_BSON = False
else:
    # The BSON module provided by pymongo works through its "BSON" class.
    if hasattr(bson, "BSON"):
        log.info("HAVE ATTRIBUTE BSON.")
        bson_encode = lambda e: bson.BSON.encode(e)
    # The BSON module provided by "pip install bson" works through the
    # "loads" function (just like pickle etc.)
    elif hasattr(bson, "loads"):
        log.info("HAVE ATTRIBUTE LOADS.")
        bson_encode = lambda e: bson.dumps(e)
    elif not hasattr(bson, "int64") or not hasattr(bson.int64, "Int64"):
        HAVE_BSON = False
    else:
        HAVE_BSON = False

# Quick and dirty way to categorize some syscalls
SOCKET_SYSCALL_FILESYSTEM_NAMES = set(("read", "write", "open", "close", "stat", "fstat",
                                   "lstat", "poll", "lseek", "access","mkdir", "rename",
                                   "rmdir", "creat", "link", "unlink", "symlink", "readlink",
                                   "chmod", "fchmod", "chown", "fchown", "lchown","umask",
                                   ))
SOCKET_SYSCALL_PROCESS_NAMES = set(("mmap", "mprotect", "brk", "clone", "fork", "vfork",
                                "execve", "exit", "kill","chdir","fchdir","sysinfo","ptrace"
                                ))

def get_key(pos):
    return pos*2

def get_value(pos):
    return pos*2+1

class ResultLogger():
    def __init__(self):
        self.config = None
        self.ip = None
        self.port = None
        self.socket = None
        self.send_lock = Lock()
        self.logtbl_explained = []
        self.syscalls = SYSCALL_NAMES
        for i in range(0,512):
            self.logtbl_explained.append(0)
        self.start = None
        self.pid = -1

    def announce_netlog(self):
        buf = "BSON\n"
        self.log_raw_direct(buf)
        
    def log_raw_direct(self, buf):
        total = 0
        while total < len(buf):
            sent = self.socket.send(buf[total:])
            if sent == 0:
                raise RuntimeError("socket connection broken")
            total = total + sent
            
    def loq(self, index, name, is_success, return_value, fmt, args):
        with self.send_lock:
            # Prepare structure explanation to be send in dictionary.
            try:
                if self.logtbl_explained[index] == 0:
                    self.logtbl_explained[index] = 1
                    buf = {}
                    buf["I"] = index
                    buf["name"] = name
                    buf["type"] = "info"
                    buf["category"] = self.resolve_category(name, args)
                    buf["args"] = ["is_success", "retval"]

                    # Extract arguments.
                    for i in range(0,len(fmt)):
                        val = unicode(args[get_key(i)])
                        buf["args"].append(val)
                    log.debug("Sending: %s", str(buf))

                    # Send BSON data to resultserver
                    self.log_raw_direct(bson_encode(buf))

            except IndexError as e:
                log.error("Syscall array probably to small: %s", e)

            # Thread info
            buf = {}
            buf["I"] = index
            buf["T"] = self.pid
            buf["t"] = time.time() - self.start
            buf["args"] = [is_success, return_value]

            # Extract argument values.
            for i in range(0,len(fmt)):
                if fmt[i] == "l":
                    val = long(args[get_value(i)])
                elif fmt[i] == "u":
                    val = unicode(args[get_value(i)])
                buf["args"].append(val)
            log.debug("Sending: %s", str(buf))

            # Send BSON data to resultserver
            self.log_raw_direct(bson_encode(buf))

    def log_new_process(self,pid,ppid,path=None):
        self.pid = pid

        arguments = ["TimeStamp", long(round(time.time() * 1000)),
                "ProcessIdentifier", pid,
                "ParentProcessIdentifier", ppid,
                "ModulePath", path]

        self.loq(511,"__process__",1,0, self.log_convert_types(arguments),arguments)

    def log_init(self, start):
        '''Establish a connection to resultserver.
        @return: connection result (true or false)'''
        # Parse the analysis configuration file generated by the agent.
        self.config = Config(cfg="analysis.conf")

        # Extract result server IP and Port. 
        self.ip = self.config.ip
        self.port = self.config.port

        # Save process init time.
        self.start = start

        try:
            # Create socket.
            self.socket = socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM)

            # Connect to result server
            self.socket.connect((self.ip, self.port))
        except socket.error as e:
            log.error(e)
            return False

        self.announce_netlog()

        return True

    def log_resolve_index(self, name):
        for index, value in self.syscalls.items():
            if value == name:
                return index
        return -1

    def log_convert_types(self, args):
        fmt = ""
        for i in range(1,len(args),2):
            if isinstance(args[i], (int, long)):
                fmt = fmt + "l"
            else:
                fmt = fmt + "u"
        return fmt

    def resolve_category(self, name, args):
        if name in SOCKET_SYSCALL_NAMES:
            return "network"
        elif name in SOCKET_SYSCALL_FILESYSTEM_NAMES:
            return "filesystem"
        elif name in SOCKET_SYSCALL_PROCESS_NAMES:
            return "process"
        else:
            for arg in args:
                if isinstance(arg, str):
                    if "filename" in arg:
                        return "filesystem"
        return "unkown"
