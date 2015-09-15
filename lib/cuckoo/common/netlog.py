# Copyright (C) 2010-2015 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import datetime
import hashlib
import logging
import os.path
import struct

try:
    import bson
    HAVE_BSON = True
except ImportError:
    HAVE_BSON = False
else:
    # The BSON module provided by pymongo works through its "BSON" class.
    if hasattr(bson, "BSON"):
        bson_decode = lambda d: bson.BSON(d).decode()
    # The BSON module provided by "pip install bson" works through the
    # "loads" function (just like pickle etc.)
    elif hasattr(bson, "loads"):
        bson_decode = lambda d: bson.loads(d)
    elif not hasattr(bson, "int64") or not hasattr(bson.int64, "Int64"):
        HAVE_BSON = False
    else:
        HAVE_BSON = False

from lib.cuckoo.common.utils import get_filename_from_path
from lib.cuckoo.common.exceptions import CuckooResultError

log = logging.getLogger(__name__)

###############################################################################
# Generic BSON based protocol - by rep
# Allows all kinds of languages / sources to generate input for Cuckoo,
# thus we can reuse report generation / signatures for other API trace sources.
###############################################################################

TYPECONVERTERS = {
    "p": lambda v: pointer_converter(v),
}

# 20 Mb max message length.
MAX_MESSAGE_LENGTH = 20 * 1024 * 1024

def pointer_converter(v):
    # If it's a 64-bit pointer treat it as such. Note that 0xffffffff is also
    # an often-used pointer so we have to handle negative pointers as well.
    if isinstance(v, bson.int64.Int64):
        return "0x%016x" % (v % 2**64)
    else:
        return "0x%08x" % (v % 2**32)

def default_converter(v):
    # Turn signed 32-bit integers into unsigned 32-bit integers. Don't convert
    # signed 64-bit integers into unsigned 64-bit integers as MongoDB doesn't
    # support unsigned 64-bit integers (and ElasticSearch probably doesn't
    # either).
    if HAVE_BSON and isinstance(v, bson.int64.Int64):
        return v

    if isinstance(v, (int, long)) and v < 0:
        return v % 2**32

    # Try to avoid various unicode issues through usage of latin-1 encoding.
    if isinstance(v, str):
        return v.decode("latin-1")
    return v

def check_names_for_typeinfo(arginfo):
    argnames = [i[0] if type(i) in (list, tuple) else i for i in arginfo]

    converters = []
    for i in arginfo:
        if type(i) in (list, tuple):
            r = TYPECONVERTERS.get(i[1], None)
            if not r:
                log.debug("Analyzer sent unknown format "
                          "specifier '{0}'".format(i[1]))
                r = default_converter
            converters.append(r)
        else:
            converters.append(default_converter)

    return argnames, converters

class BsonParser(object):
    """Handle .bson logs from monitor. Basically we would like to directly pass through
    the parsed data structures, but the .bson logs need a bit special handling to be more space efficient.

    Basically we get "info" messages that explain how the function arguments will come through later on.
    This class remembers these info mappings and then transforms the api call messages accordingly.

    Other message types typically get passed through after renaming the keys slightly.
    """

    def __init__(self, fd):
        self.fd = fd
        self.infomap = {}
        self.flags_value = {}
        self.flags_bitmask = {}
        self.pid = None

        if not HAVE_BSON:
            log.critical("Starting BsonParser, but bson is not available! (install with `pip install bson`)")

    def close(self):
        pass

    def resolve_flags(self, apiname, argdict, flags):
        # Resolve 1:1 values.
        for argument, values in self.flags_value[apiname].items():
            if isinstance(argdict[argument], str):
                value = int(argdict[argument], 16)
            else:
                value = argdict[argument]
            if value in values:
                flags[argument] = values[value]

        # Resolve bitmasks.
        for argument, values in self.flags_bitmask[apiname].items():
            if argument in flags:
                continue

            flags[argument] = []

            if isinstance(argdict[argument], str):
                value = int(argdict[argument], 16)
            else:
                value = argdict[argument]

            for key, flag in values:
                # TODO Have the monitor provide actual bitmasks as well.
                if (value & key) == key:
                    flags[argument].append(flag)

            flags[argument] = "|".join(flags[argument])

    def __iter__(self):
        self.fd.seek(0)

        while True:
            data = self.fd.read(4)
            if not data:
                return

            if not len(data) == 4:
                log.critical("BsonParser lacking data.")
                return

            blen = struct.unpack("I", data)[0]
            if blen > MAX_MESSAGE_LENGTH:
                log.critical("BSON message larger than MAX_MESSAGE_LENGTH, "
                             "stopping handler.")
                return

            data += self.fd.read(blen-4)
            if len(data) < blen:
                log.critical("BsonParser lacking data.")
                return

            try:
                dec = bson_decode(data)
            except Exception as e:
                log.warning("BsonParser decoding problem {0} on "
                            "data[:50] {1}".format(e, repr(data[:50])))
                return

            mtype = dec.get("type", "none")
            index = dec.get("I", -1)

            if mtype == "info":
                # API call index info message, explaining the argument names, etc.
                name = dec.get("name", "NONAME")
                arginfo = dec.get("args", [])
                category = dec.get("category")

                argnames, converters = check_names_for_typeinfo(arginfo)
                self.infomap[index] = name, arginfo, argnames, converters, category

                if dec.get("flags_value"):
                    self.flags_value[name] = {}
                    for arg, values in dec["flags_value"].items():
                        self.flags_value[name][arg] = dict(values)

                if dec.get("flags_bitmask"):
                    self.flags_bitmask[name] = {}
                    for arg, values in dec["flags_bitmask"].items():
                        self.flags_bitmask[name][arg] = values
                continue

            # Handle dumped buffers.
            if mtype == "buffer":
                buf = dec.get("buffer")
                sha1 = dec.get("checksum")

                # Why do we pass along a sha1 checksum again?
                if sha1 != hashlib.sha1(buf).hexdigest():
                    log.warning("Incorrect sha1 passed along for a buffer.")
                    continue

                # If the parent is netlogs ResultHandler then we actually dump
                # it - this should only be the case during the analysis, any
                # after proposing will then be ignored.
                from lib.cuckoo.core.resultserver import ResultHandler

                if isinstance(self.fd, ResultHandler):
                    filepath = os.path.join(self.fd.storagepath,
                                            "buffer", sha1)
                    with open(filepath, "wb") as f:
                        f.write(buf)

                continue

            tid = dec.get("T", 0)
            time = dec.get("t", 0)

            parsed = {
                "type": mtype,
                "tid": tid,
                "time": time,
            }

            if mtype == "debug":
                log.info("Debug message from monitor: {0}".format(dec.get("msg", "")))
                parsed["message"] = dec.get("msg", "")

            else:
                # Regular api call from monitor
                if index not in self.infomap:
                    log.warning("Got API with unknown index - monitor needs "
                                "to explain first: {0}".format(dec))
                    continue

                apiname, arginfo, argnames, converters, category = self.infomap[index]
                args = dec.get("args", [])

                if len(args) != len(argnames):
                    log.warning("Inconsistent arg count (compared to arg names) "
                                "on {2}: {0} names {1}".format(dec, argnames,
                                                               apiname))
                    continue

                argdict = dict((argnames[i], converters[i](args[i]))
                               for i in range(len(args)))

                # Special new process message from the monitor.
                if apiname == "__process__":
                    parsed["type"] = "process"

                    if "TimeLow" in argdict:
                        timelow = argdict["TimeLow"]
                        timehigh = argdict["TimeHigh"]

                        parsed["pid"] = pid = argdict["ProcessIdentifier"]
                        parsed["ppid"] = argdict["ParentProcessIdentifier"]
                        modulepath = argdict["ModulePath"]

                        # FILETIME is 100-nanoseconds from 1601 :/
                        vmtimeunix = (timelow + (timehigh << 32))
                        vmtimeunix = vmtimeunix / 10000000.0 - 11644473600

                    elif "time_low" in argdict:
                        timelow = argdict["time_low"]
                        timehigh = argdict["time_high"]

                        if "pid" in argdict:
                            parsed["pid"] = pid = argdict["pid"]
                            parsed["ppid"] = argdict["ppid"]
                        else:
                            parsed["pid"] = pid = argdict["process_identifier"]
                            parsed["ppid"] = argdict["parent_process_identifier"]

                        modulepath = argdict["module_path"]

                        # FILETIME is 100-nanoseconds from 1601 :/
                        vmtimeunix = (timelow + (timehigh << 32))
                        vmtimeunix = vmtimeunix / 10000000.0 - 11644473600

                    elif "TimeStamp" in argdict:
                        vmtimeunix = argdict["TimeStamp"] / 1000.0
                        vmtime = datetime.datetime.fromtimestamp(vmtimeunix)

                        parsed["pid"] = pid = argdict["ProcessIdentifier"]
                        parsed["ppid"] = argdict["ParentProcessIdentifier"]
                        modulepath = argdict["ModulePath"]

                    else:
                        raise CuckooResultError("I don't recognise the bson log contents.")

                    vmtime = datetime.datetime.fromtimestamp(vmtimeunix)
                    parsed["first_seen"] = vmtime

                    procname = get_filename_from_path(modulepath)
                    parsed["process_name"] = procname

                    self.pid = pid

                elif apiname == "__thread__":
                    parsed["pid"] = pid = argdict["ProcessIdentifier"]

                # elif apiname == "__anomaly__":
                    # tid = argdict["ThreadIdentifier"]
                    # subcategory = argdict["Subcategory"]
                    # msg = argdict["Message"]
                    # self.handler.log_anomaly(subcategory, tid, msg)
                    # return True

                else:
                    parsed["type"] = "apicall"
                    parsed["pid"] = self.pid
                    parsed["api"] = apiname
                    parsed["category"] = category
                    parsed["status"] = argdict.pop("is_success", 1)
                    parsed["return_value"] = argdict.pop("retval", 0)
                    parsed["arguments"] = argdict
                    parsed["flags"] = {}

                    parsed["stacktrace"] = dec.get("s", [])
                    parsed["uniqhash"] = dec.get("h", 0)

                    if "e" in dec and "E" in dec:
                        parsed["last_error"] = dec["e"]
                        parsed["nt_status"] = dec["E"]

                    if apiname in self.flags_value:
                        self.resolve_flags(apiname, argdict, parsed["flags"])

            yield parsed
