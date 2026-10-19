"""Exact invocation ownership; suspended Windows launch closes the assignment race."""
import ctypes
import os
from pathlib import Path
import subprocess
import time

class OwnedJob:
    """Windows kernel ownership for descendants, CPU accounting and cleanup.

    Queries touch only a job we just created, never enumerate system processes.
    """
    def __init__(self, process):
        self.handle = None
        self.process = process
        self.seen = {process.pid}
        self.killed = False
        self.exit_states = []
        if os.name == "nt":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
            kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
            self.kernel = kernel
            self.handle = kernel.CreateJobObjectW(None, None)
            class BasicLimits(ctypes.Structure):
                _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                            ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                            ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                            ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                            ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.handle or not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) \
                    or not kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
                if self.handle:
                    kernel.CloseHandle(self.handle)
                self.handle = None
                raise RuntimeError("cannot_assign_owned_process_job")

    def pids(self):
        if not self.handle:
            return [self.process.pid] if self.process.poll() is None else []
        class Pids(ctypes.Structure):
            _fields_ = [("assigned", ctypes.c_uint32), ("count", ctypes.c_uint32), ("ids", ctypes.c_size_t * 512)]
        value = Pids()
        if not self.kernel.QueryInformationJobObject(self.handle, 3, ctypes.byref(value), ctypes.sizeof(value), None):
            raise RuntimeError("cannot_query_owned_process_job")
        current = list(value.ids[:value.count])
        self.seen.update(current)
        return current

    def exit_state(self, pid, held):
        """Classify a sampled owned PID with finite, nonblocking state checks.

        Job IDs may still contain an exited process, and image lookup can race
        exit. No access failure or unknown live child proves natural completion.
        Retain opened handles through termination to avoid PID reuse races.
        """
        from ctypes import wintypes
        handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not handle:
            # Resample this exact job: the sampled process may already be gone.
            return "departed" if pid not in self.pids() else "unresolved"
        held.append(handle)
        wait = self.kernel.WaitForSingleObject(handle, 0)
        if wait == 0:
            return "signaled"
        if wait != 258:  # WAIT_TIMEOUT is the only proof of a live handle.
            return "unresolved"
        image = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(image))
        system = ctypes.create_unicode_buffer(32768)
        system_ok = 0 < self.kernel.GetSystemDirectoryW(system, len(system)) < len(system)
        image_ok = self.kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length))
        # A process can signal during either image query, including failed ones.
        wait = self.kernel.WaitForSingleObject(handle, 0)
        if wait == 0:
            return "signaled"
        if wait != 258:
            return "unresolved"
        if system_ok and image_ok:
            if os.path.normcase(image.value) == os.path.normcase(str(Path(system.value) / "conhost.exe")):
                return "console_host"
            return "live_payload"
        return "unresolved"

    def close(self):
        if not self.handle:
            return
        held = []
        try:
            current = self.pids()
            for pid in current:
                state = self.exit_state(pid, held)
                self.exit_states.append({"pid": pid, "state": state})
                self.killed |= state not in {"departed", "signaled", "console_host"}
            if current:
                if not self.kernel.TerminateJobObject(self.handle, 1):
                    raise RuntimeError("cannot_terminate_owned_process_job")
                for handle in held:
                    if self.kernel.WaitForSingleObject(handle, 0xFFFFFFFF) != 0:
                        raise RuntimeError("cannot_confirm_owned_process_exit")
                while self.pids():
                    time.sleep(0.05)
        finally:
            for handle in held:
                self.kernel.CloseHandle(handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def start(args, **kwargs):
    if os.name != "nt":
        raise RuntimeError("OpenCode exact process ownership requires Windows.")
    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS | 0x4
    process = subprocess.Popen(args, **kwargs)
    try:
        process._owned_job = OwnedJob(process)
        from ctypes import wintypes
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = [wintypes.HANDLE]
        resume.restype = ctypes.c_long
        if resume(wintypes.HANDLE(int(process._handle))) < 0:
            raise RuntimeError("cannot_resume_owned_process")
        return process
    except BaseException:
        try:
            if hasattr(process, "_owned_job"):
                process._owned_job.close()
        finally:
            # Held Popen handle, never taskkill or a PID lookup. Still suspended on assignment failure.
            if process.poll() is None:
                process.kill()
            process.wait()
            process._handle.Close()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise RuntimeError("OpenCode process ownership could not be established.") from None


def stop(process):
    previous = getattr(process, "_owned_cleanup", None)
    if previous is not None:
        return previous
    job = process._owned_job
    verified = False
    try:
        job.close()
        verified = True
    except Exception:
        # Closing the kill-on-close job still enforces containment, but a
        # failed query/termination check never becomes a successful receipt.
        verified = False
    finally:
        process.wait()
        process._handle.Close()
    result = {"stopped": verified, "natural_exit": verified and not job.killed,
              "exit_states": job.exit_states, "owned_pids": sorted(job.seen)}
    process._owned_cleanup = result
    return result
