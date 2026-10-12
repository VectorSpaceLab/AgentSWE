#!/usr/bin/env python3
"""Run submitted repository code behind filesystem and syscall restrictions."""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import resource
import runpy
import sys


def sandbox(read_paths, write_paths):
    if sys.platform != 'linux' or platform.machine() != 'x86_64' or os.geteuid() != 0:
        raise RuntimeError('repository sandbox requires Linux x86-64 and a privileged trusted parent')
    libc = ctypes.CDLL(None, use_errno=True)
    def checked(value):
        if value < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return value
    checked(libc.prctl(38, 1, 0, 0, 0))
    abi = checked(libc.syscall(444, 0, 0, 1))
    class Ruleset(ctypes.Structure):
        _fields_ = [('access', ctypes.c_uint64)]
    class Rule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('access', ctypes.c_uint64), ('fd', ctypes.c_int)]
    access = (1 << 13) - 1
    rules = Ruleset(access)
    fd = checked(libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0))
    try:
        paths = {Path(p).resolve(): ((1 << 2) | (1 << 3)) for p in read_paths if Path(p).exists()}
        paths.update({Path(p).resolve(): access & ~(1 << 0) for p in write_paths})
        for path, rights in paths.items():
            if path == Path('/'):
                raise RuntimeError('refusing unrestricted filesystem root')
            if path.is_file():
                rights &= (1 << 2)
            target = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = Rule(rights, target)
                checked(libc.syscall(445, fd, 1, ctypes.byref(rule), 0))
            finally:
                os.close(target)
        checked(libc.syscall(446, fd, 0))
    finally:
        os.close(fd)
    denied = {41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,57,58,59,62,101,155,161,165,166,167,168,169,175,176,200,234,246,248,249,250,272,282,284,298,299,303,304,307,308,310,311,313,317,319,321,322,323,424,425,426,427,428,429,430,431,432,433,434,438,440,442}
    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte), ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]
    class Program(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('filter', ctypes.POINTER(Filter))]
    reject = 0x00050000 | errno.EPERM
    instructions = [(0x20,0,0,4),(0x15,1,0,0xC000003E),(0x06,0,0,0x80000000),(0x20,0,0,0),(0x35,0,1,0x40000000),(0x06,0,0,reject)]
    for number in sorted(denied):
        instructions += [(0x15,0,1,number),(0x06,0,0,reject)]
    # clone3 ENOSYS permits libc's threaded clone fallback; clone must share
    # the VM/thread group, so a submitted program cannot fork a new process.
    instructions += [(0x15,0,1,435),(0x06,0,0,0x00050000 | errno.ENOSYS),
                     (0x15,0,4,56),(0x20,0,0,16),(0x54,0,0,0x10100),
                     (0x15,1,0,0x10100),(0x06,0,0,reject),(0x06,0,0,0x7FFF0000)]
    array = (Filter * len(instructions))(*(Filter(*row) for row in instructions))
    program = Program(len(instructions), array)
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)
    checked(libc.prctl(22, 2, ctypes.byref(program), 0, 0))
    return {'landlock_abi': abi, 'seccomp': True, 'uid': os.geteuid(), 'network': 'denied', 'processes': 'denied', 'writes': [str(p) for p in write_paths]}


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024**2, 128 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        isolation = sandbox([sys.base_prefix, '/usr/lib', '/lib', '/lib64', config['repository'], config['script']], config['write_paths'])
    except Exception as exc:
        print(json.dumps({'infrastructure_error': type(exc).__name__ + ': ' + str(exc)}), flush=True)
        return 125
    print(json.dumps({'sandbox_ready': isolation}), flush=True)
    if config.get('health_only'):
        return 0
    sys.path.insert(0, str(Path(config['repository']) / 'src'))
    sys.argv = config['argv']
    runpy.run_path(config['script'], run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
