"""Process jail for the worker browser (audit H4): no external application may start.

Chrome hands some URLs (``mailto:`` above all) to the operating system, which then
starts the user's default handler -- e.g. a personal browser or mail client -- outside
every ApplyPilot boundary. Profile preferences do not reliably prevent this.

On Windows the worker Chrome is started *suspended*, placed in a Job Object and only
then resumed, so it and every process it ever creates belong to the job (no
breakaway). A monitor thread receives the job's new-process notifications and
terminates, immediately, any process whose image is not the configured Chrome
executable, then reports an escape (the network guard fails closed). The job is
also ``KILL_ON_JOB_CLOSE``: if the launcher dies, the browser dies with it.

This contains handlers that the shell starts as children of Chrome (the classic
``shell\\open\\command`` registrations). A handler started out-of-process by the
shell (COM ``DelegateExecute``) or a running application receiving the URL over
its own IPC would not be a child; that residual path is documented as unverified
and is why tests never trigger an OS protocol handler.

Non-Windows platforms get no jail (``jail_supported()`` is False); the launcher
reports that, and live apply sessions there are not certified.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_JobObjectAssociateCompletionPortInformation = 7
_JOB_OBJECT_MSG_NEW_PROCESS = 6
_JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO = 4
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_INFINITE = 0xFFFFFFFF


def jail_supported() -> bool:
    return sys.platform == "win32"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


class ProcessJail:
    """One job object for one worker browser. ``allowed_images`` are full executable paths."""

    def __init__(self, allowed_images: list[str], on_escape: Callable[[str], None]) -> None:
        if not jail_supported():
            raise OSError("process jail requires Windows")
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        self._wt = wintypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.k32.CreateIoCompletionPort.restype = wintypes.HANDLE
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.allowed = {_norm(p) for p in allowed_images}
        self.on_escape = on_escape
        self.escapes: list[str] = []
        self.closed = False
        self.job = self.k32.CreateJobObjectW(None, None)
        if not self.job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self.port = self.k32.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
        if not self.port:
            raise OSError(ctypes.get_last_error(), "CreateIoCompletionPort failed")
        self._configure()
        self._thread = threading.Thread(target=self._monitor, name="proc-jail", daemon=True)
        self._thread.start()

    # --- setup ---------------------------------------------------------------------
    def _configure(self) -> None:
        ct, wt = self._ct, self._wt

        class IO_COUNTERS(ct.Structure):
            _fields_ = [(n, ct.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",  # noqa: RUF012
                                                      "OtherOperationCount", "ReadTransferCount",
                                                      "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ct.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ct.c_longlong), ("PerJobUserTimeLimit", ct.c_longlong),  # noqa: RUF012
                        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ct.c_size_t),
                        ("MaximumWorkingSetSize", ct.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ct.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

        class EXTENDED(ct.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),  # noqa: RUF012
                        ("ProcessMemoryLimit", ct.c_size_t), ("JobMemoryLimit", ct.c_size_t),
                        ("PeakProcessMemoryUsed", ct.c_size_t), ("PeakJobMemoryUsed", ct.c_size_t)]

        class PORTINFO(ct.Structure):
            _fields_ = [("CompletionKey", ct.c_void_p), ("CompletionPort", wt.HANDLE)]  # noqa: RUF012

        ext = EXTENDED()
        # No JOB_OBJECT_LIMIT_BREAKAWAY_OK / SILENT_BREAKAWAY_OK: nothing may leave the job.
        ext.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k32.SetInformationJobObject(self.job, _JobObjectExtendedLimitInformation,
                                                ct.byref(ext), ct.sizeof(ext)):
            raise OSError(ct.get_last_error(), "SetInformationJobObject(limits) failed")
        port = PORTINFO(None, self.port)
        if not self.k32.SetInformationJobObject(self.job, _JobObjectAssociateCompletionPortInformation,
                                                ct.byref(port), ct.sizeof(port)):
            raise OSError(ct.get_last_error(), "SetInformationJobObject(port) failed")

    def spawn(self, cmd: list[str], **kwargs) -> subprocess.Popen:
        """Start ``cmd`` suspended, put it in the job, then let it run."""
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_SUSPENDED
        proc = subprocess.Popen(cmd, **kwargs)
        handle = int(proc._handle)  # type: ignore[attr-defined]
        try:
            if not self.k32.AssignProcessToJobObject(self.job, self._wt.HANDLE(handle)):
                raise OSError(self._ct.get_last_error(), "AssignProcessToJobObject failed")
            ntdll = self._ct.WinDLL("ntdll")
            if ntdll.NtResumeProcess(self._wt.HANDLE(handle)) != 0:
                raise OSError("NtResumeProcess failed")
        except Exception:
            proc.kill()
            raise
        return proc

    def contains(self, pid: int) -> bool:
        h = self.k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            res = self._wt.BOOL()
            ok = self.k32.IsProcessInJob(self._wt.HANDLE(h), self.job, self._ct.byref(res))
            return bool(ok and res.value)
        finally:
            self.k32.CloseHandle(self._wt.HANDLE(h))

    # --- monitor ---------------------------------------------------------------------
    def _image(self, h) -> str | None:
        buf = self._ct.create_unicode_buffer(32768)
        size = self._wt.DWORD(len(buf))
        if self.k32.QueryFullProcessImageNameW(h, 0, buf, self._ct.byref(size)):
            return buf.value
        return None

    def _check(self, pid: int) -> None:
        h = self.k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_TERMINATE, False, pid)
        if not h:
            err = self._ct.get_last_error()
            if err == _ERROR_ACCESS_DENIED:
                self._escape(f"process {pid} in the browser job could not be inspected or stopped")
            return  # already gone
        try:
            image = self._image(self._wt.HANDLE(h))
            if image is not None and _norm(image) in self.allowed:
                return
            self.k32.TerminateProcess(self._wt.HANDLE(h), 1)
            self._escape(f"browser started an external program ({os.path.basename(image or '?')}); "
                         "it was terminated")
        finally:
            self.k32.CloseHandle(self._wt.HANDLE(h))

    def _escape(self, why: str) -> None:
        self.escapes.append(why)
        logger.warning("process jail: %s", why)
        try:
            self.on_escape(why)
        except Exception:
            logger.exception("process jail escape callback failed")

    def _monitor(self) -> None:
        ct, wt = self._ct, self._wt
        msg, key, ov = wt.DWORD(), ct.c_size_t(), ct.c_void_p()
        while not self.closed:
            ok = self.k32.GetQueuedCompletionStatus(self.port, ct.byref(msg), ct.byref(key),
                                                    ct.byref(ov), wt.DWORD(500))
            if not ok and ov.value is None:
                continue  # timeout
            if msg.value == _JOB_OBJECT_MSG_NEW_PROCESS and ov.value:
                self._check(int(ov.value))

    def close(self) -> None:
        """Terminate everything still in the job and release the handles."""
        if self.closed:
            return
        self.closed = True
        try:
            self.k32.TerminateJobObject(self.job, 1)
        finally:
            self._thread.join(timeout=2)
            self.k32.CloseHandle(self.port)
            self.k32.CloseHandle(self.job)
