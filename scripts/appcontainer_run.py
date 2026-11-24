"""Run a Windows program in a disposable, capability-free AppContainer."""

import argparse
import ctypes as ct
from ctypes import wintypes as wt
from contextlib import ExitStack
import json
import msvcrt
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid


kernel = ct.WinDLL("kernel32", use_last_error=True)
userenv = ct.WinDLL("userenv", use_last_error=True)
advapi = ct.WinDLL("advapi32", use_last_error=True)
P = ct.c_void_p
H = wt.HANDLE
D = wt.DWORD
SIZE = ct.c_size_t


def function(dll, name, result, *params):
    fn = getattr(dll, name)
    fn.restype = result
    fn.argtypes = params
    return fn


class Startup(ct.Structure):
    _fields_ = [
        ("cb", D), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR), ("dwX", D), ("dwY", D), ("dwXSize", D),
        ("dwYSize", D), ("dwXCountChars", D), ("dwYCountChars", D),
        ("dwFillAttribute", D), ("dwFlags", D), ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD), ("lpReserved2", P), ("hStdInput", H),
        ("hStdOutput", H), ("hStdError", H),
    ]


class StartupEx(ct.Structure):
    _fields_ = [("startup", Startup), ("attrs", P)]


class ProcessInfo(ct.Structure):
    _fields_ = [("process", H), ("thread", H), ("pid", D), ("tid", D)]


class SecurityCapabilities(ct.Structure):
    _fields_ = [("container_sid", P), ("capabilities", P), ("count", D), ("reserved", D)]


class Limits(ct.Structure):
    _fields_ = [
        ("per_process_time", ct.c_int64), ("per_job_time", ct.c_int64),
        ("flags", D), ("min_working", SIZE), ("max_working", SIZE),
        ("max_processes", D), ("affinity", SIZE), ("priority", D), ("scheduling", D),
    ]


class ExtendedLimits(ct.Structure):
    _fields_ = [
        ("basic", Limits), ("io", ct.c_uint64 * 6), ("process_mem", SIZE),
        ("job_mem", SIZE), ("peak_process_mem", SIZE), ("peak_job_mem", SIZE),
    ]


create_profile = function(userenv, "CreateAppContainerProfile", wt.LONG, wt.LPCWSTR,
                          wt.LPCWSTR, wt.LPCWSTR, P, D, ct.POINTER(P))
delete_profile = function(userenv, "DeleteAppContainerProfile", wt.LONG, wt.LPCWSTR)
free_sid = function(advapi, "FreeSid", P, P)
convert_sid = function(advapi, "ConvertSidToStringSidW", wt.BOOL, P, ct.POINTER(P))
equal_sid = function(advapi, "EqualSid", wt.BOOL, P, P)
local_free = function(kernel, "LocalFree", H, H)
close = function(kernel, "CloseHandle", wt.BOOL, H)
create_job = function(kernel, "CreateJobObjectW", H, P, wt.LPCWSTR)
set_job = function(kernel, "SetInformationJobObject", wt.BOOL, H, ct.c_int, P, D)
kill_job = function(kernel, "TerminateJobObject", wt.BOOL, H, wt.UINT)
init_attrs = function(kernel, "InitializeProcThreadAttributeList", wt.BOOL, P, D, D,
                      ct.POINTER(SIZE))
delete_attrs = function(kernel, "DeleteProcThreadAttributeList", None, P)
add_attr = function(kernel, "UpdateProcThreadAttribute", wt.BOOL, P, D, SIZE, P, SIZE, P, P)
create_process = function(kernel, "CreateProcessW", wt.BOOL, wt.LPCWSTR, wt.LPWSTR,
                          P, P, wt.BOOL, D, P, wt.LPCWSTR, ct.POINTER(StartupEx),
                          ct.POINTER(ProcessInfo))
resume = function(kernel, "ResumeThread", D, H)
wait = function(kernel, "WaitForSingleObject", D, H, D)
exit_code = function(kernel, "GetExitCodeProcess", wt.BOOL, H, ct.POINTER(D))
open_token = function(advapi, "OpenProcessToken", wt.BOOL, H, D, ct.POINTER(H))
get_token = function(advapi, "GetTokenInformation", wt.BOOL, H, ct.c_int, P, D, ct.POINTER(D))


def check(value, operation):
    if not value:
        raise OSError(f"{operation}: {ct.WinError(ct.get_last_error())}")
    return value


def hresult(code, operation):
    if code < 0:
        raise OSError(f"{operation}: HRESULT 0x{code & 0xffffffff:08x}")


def private_folder(stage, relative):
    if not relative or Path(relative).is_absolute():
        raise ValueError(f"Expected a relative staging folder: {relative!r}")
    path = stage / relative
    if ".." in Path(relative).parts or path.resolve() == stage or not path.is_dir():
        raise ValueError(f"Invalid staging folder: {relative!r}")
    for directory in (path, *path.parents):
        if directory.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Reparse point in staging path: {directory}")
        if directory == stage:
            break
    return path.resolve()


def check_grant_tree(path):
    def fail(error):
        raise error
    for parent, dirs, files in os.walk(path, onerror=fail):
        for name in dirs + files:
            item = Path(parent, name)
            info = item.stat(follow_symlinks=False)
            if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"Refusing a grant across reparse point {item}")
            if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
                raise ValueError(f"Refusing a grant on a shared hardlink {item}")


def acl(path, action, argument):
    tool = Path(os.environ["SystemRoot"], "System32", "icacls.exe")
    subprocess.run([str(tool), str(path), action, argument, "/Q"], check=True)


def verify_token(process, sid):
    token = H()
    check(open_token(process, 0x8, ct.byref(token)), "OpenProcessToken")
    try:
        def info(kind):
            size = D()
            get_token(token, kind, None, 0, ct.byref(size))
            buf = ct.create_string_buffer(size.value)
            check(get_token(token, kind, buf, len(buf), ct.byref(size)), "GetTokenInformation")
            return buf
        is_container = D.from_buffer(info(29)).value
        capability_count = D.from_buffer(info(30)).value
        container_info = info(31)
        if is_container != 1 or capability_count or not equal_sid(P.from_buffer(container_info), sid):
            raise RuntimeError("The suspended process did not receive the expected restricted token.")
    finally:
        close(token)


def run(args):
    original_stage = Path(args.stage).absolute()
    for ancestor in [original_stage, *original_stage.parents]:
        if ancestor.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Reparse point in stage path: {ancestor}")
    stage = original_stage.resolve(strict=True)
    if not (stage / ".victory-appcontainer-stage").is_file():
        raise ValueError(f"Missing private staging marker in {stage}")
    if not args.command or args.command[0] != "--" or len(args.command) < 2:
        raise ValueError("Supply -- followed by an absolute executable path and its arguments.")
    command = args.command[1:]
    if not Path(command[0]).is_absolute():
        raise ValueError("Executable must be an absolute path.")
    exe = Path(command[0]).resolve(strict=True)
    read = [private_folder(stage, value) for value in args.read]
    writable = [private_folder(stage, value) for value in args.write]
    cwd = private_folder(stage, args.cwd)
    grants = read + writable
    reserved_logs = stage / "logs"
    for i, path in enumerate(grants):
        if path.is_relative_to(reserved_logs) or reserved_logs.is_relative_to(path):
            raise ValueError("Keep logs outside guest grants.")
        if any(path.is_relative_to(other) or other.is_relative_to(path) for other in grants[:i]):
            raise ValueError("Specify disjoint guest grant directories.")
        check_grant_tree(path)
    if not any(cwd.is_relative_to(path) for path in grants):
        raise ValueError("The working directory must be inside a granted folder.")
    system32 = Path(os.environ["SystemRoot"], "System32")
    if not exe.is_relative_to(system32) and not any(exe.is_relative_to(path) for path in read):
        raise ValueError("Executable must be in a read-only staged directory or System32.")
    if not 0 < args.timeout <= 86400:
        raise ValueError("Timeout must be between one second and one day.")
    env = {name: os.environ[name] for name in ("SystemRoot", "SystemDrive", "ComSpec", "windir",
                                               "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA")
           if name in os.environ}
    env["PATH"] = str(system32)
    if writable:
        env.update(TEMP=str(writable[0]), TMP=str(writable[0]))
    for entry in args.env:
        key, sep, value = entry.partition("=")
        if not key or not sep:
            raise ValueError("Environment overrides must be NAME=value.")
        env[key] = value
    envbuf = ct.create_unicode_buffer("\0".join(f"{name}={value}" for name, value in
                                                sorted(env.items(), key=lambda item: item[0].upper())) + "\0")
    profile = "Victory.Control." + uuid.uuid4().hex
    if reserved_logs.exists() and reserved_logs.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("The logs directory must not be a reparse point.")
    log = reserved_logs / profile
    log.mkdir(parents=True)
    print(f"Logs: {log}", flush=True)
    with ExitStack() as cleanup:
        sid = P()
        hresult(create_profile(profile, profile, "Disposable game renderer", None, 0,
                               ct.byref(sid)), "CreateAppContainerProfile")
        cleanup.callback(lambda: hresult(delete_profile(profile), "DeleteAppContainerProfile"))
        cleanup.callback(free_sid, sid)
        string_sid = P()
        check(convert_sid(sid, ct.byref(string_sid)), "ConvertSidToStringSid")
        trustee = "*" + ct.wstring_at(string_sid)
        local_free(string_sid)
        for path in grants:
            rights = "M" if path in writable else "RX"
            cleanup.callback(acl, path, "/remove:g", trustee)
            acl(path, "/grant", f"{trustee}:(OI)(CI){rights}")

        job = check(create_job(None, None), "CreateJobObject")
        cleanup.callback(close, job)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        check(set_job(job, 9, ct.byref(limits), ct.sizeof(limits)), "SetInformationJobObject")
        attr_size = SIZE()
        init_attrs(None, 3, 0, ct.byref(attr_size))
        attrs = ct.create_string_buffer(attr_size.value)
        check(init_attrs(attrs, 3, 0, ct.byref(attr_size)), "InitializeProcThreadAttributeList")
        cleanup.callback(delete_attrs, attrs)
        caps = SecurityCapabilities(sid, None, 0, 0)
        jobs = (H * 1)(job)
        std = [cleanup.enter_context(open(path, mode, buffering=0)) for path, mode in
               [(os.devnull, "rb"), (log / "stdout.log", "xb"), (log / "stderr.log", "xb")]]
        handles = (H * 3)(*(msvcrt.get_osfhandle(f.fileno()) for f in std))
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        for attr, value in [(0x20009, caps), (0x20002, handles), (0x2000D, jobs)]:
            check(add_attr(attrs, 0, attr, ct.byref(value), ct.sizeof(value), None, None),
                  "UpdateProcThreadAttribute")
        startup = StartupEx()
        startup.startup.cb = ct.sizeof(startup)
        startup.startup.dwFlags = 0x100  # STARTF_USESTDHANDLES
        startup.startup.hStdInput, startup.startup.hStdOutput, startup.startup.hStdError = handles
        startup.attrs = ct.cast(attrs, P)
        info = ProcessInfo()
        cmdline = ct.create_unicode_buffer(subprocess.list2cmdline(command))
        flags = 0x80000 | 0x400 | 0x08000000 | 0x4  # extended, Unicode env, no console, suspended
        check(create_process(str(exe), cmdline, None, None, True, flags, envbuf, str(cwd),
                             ct.byref(startup), ct.byref(info)), "CreateProcess")
        cleanup.callback(close, info.process)
        cleanup.callback(close, info.thread)
        for handle in handles:
            os.set_handle_inheritable(handle, False)
        verify_token(info.process, sid)
        started = time.monotonic()
        if resume(info.thread) == 0xffffffff:
            raise ct.WinError(ct.get_last_error())
        print(f"PID {info.pid} verified AppContainer with 0 capabilities", flush=True)
        outcome = wait(info.process, args.timeout * 1000)
        if outcome not in (0, 0x102):
            raise ct.WinError(ct.get_last_error())
        timed_out = outcome == 0x102
        if timed_out:
            check(kill_job(job, 124), "TerminateJobObject")
            if wait(info.process, 10000) != 0:
                raise RuntimeError("Timed-out process did not terminate in ten seconds.")
        code = D()
        check(exit_code(info.process, ct.byref(code)), "GetExitCodeProcess")
        result = dict(pid=info.pid, capabilities=0, timed_out=timed_out,
                      elapsed_seconds=round(time.monotonic() - started, 2), exit_code=code.value)
        (log / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)
        return 124 if timed_out else code.value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--read", action="append", default=[])
    parser.add_argument("--write", action="append", default=[])
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    try:
        sys.exit(run(parser.parse_args()))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
