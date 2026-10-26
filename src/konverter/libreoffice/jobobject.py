"""Запуск внешнего процесса в объекте задания Windows (Job Object).

Зачем: `soffice.exe` — лаунчер, который запускает `soffice.bin` и ждёт его;
убить только лаунчер мало — останется дерево. Объект задания позволяет убить
всё дерево разом (`TerminateJobObject`), а флаг KILL_ON_JOB_CLOSE — что
Windows сама убьёт процессы, если наша программа аварийно завершится
(закроется последний дескриптор задания).

Порядок запуска без гонки: процесс создаётся приостановленным
(`CREATE_SUSPENDED`), назначается в задание, и только потом возобновляется
(`NtResumeProcess`) — его потомки наследуют задание автоматически.

Лимиты задания: память всего задания (`JOB_OBJECT_LIMIT_JOB_MEMORY`),
`DIE_ON_UNHANDLED_EXCEPTION` (без окна «программа не отвечает» / WER).
Отмена и таймаут проверяются каждые `POLL_INTERVAL` с. После любой развязки
(норма, ошибка, отмена, таймаут) задание закрывается через
`TerminateJobObject` и проверяется, что в нём не осталось процессов.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Sequence

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.25

_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_LIMIT_JOB_MEMORY = 0x00000200
_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_EXTENDED_LIMIT_INFORMATION = 9
_JOB_BASIC_ACCOUNTING_INFORMATION = 1
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BASIC_ACCOUNTING(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.QueryInformationJobObject.restype = wintypes.BOOL
    k.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


class JobObject:
    """Объект задания с KILL_ON_JOB_CLOSE и (необязательно) лимитом памяти."""

    def __init__(self, memory_limit: int | None = None) -> None:
        if os.name != "nt":
            raise OSError("объекты задания есть только в Windows")
        self._k = _kernel32()
        handle = self._k.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        info = _EXTENDED_LIMIT()
        flags = _LIMIT_KILL_ON_JOB_CLOSE | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        if memory_limit:
            flags |= _LIMIT_JOB_MEMORY
            info.JobMemoryLimit = int(memory_limit)
        info.BasicLimitInformation.LimitFlags = flags
        if not self._k.SetInformationJobObject(
            handle, _JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, pid: int) -> None:
        """Назначает процесс в задание (дескриптор открывается по PID)."""
        access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SUSPEND_RESUME | _PROCESS_QUERY_LIMITED_INFORMATION
        process = self._k.OpenProcess(access, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._k.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._k.CloseHandle(process)

    def active_processes(self) -> int:
        info = _BASIC_ACCOUNTING()
        if not self._k.QueryInformationJobObject(
            self.handle, _JOB_BASIC_ACCOUNTING_INFORMATION, ctypes.byref(info),
            ctypes.sizeof(info), None,
        ):
            return -1
        return int(info.ActiveProcesses)

    def terminate(self, wait: float = 5.0) -> bool:
        """Убивает все процессы задания; True — процессов не осталось."""
        if not self.handle:
            return True
        self._k.TerminateJobObject(self.handle, 1)
        deadline = time.monotonic() + wait
        while True:
            active = self.active_processes()
            if active == 0:
                return True
            if time.monotonic() >= deadline:
                log.warning("В объекте задания остались процессы: %s", active)
                return False
            time.sleep(0.05)

    def close(self) -> None:
        if self.handle:
            self._k.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "JobObject":
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.terminate()
        finally:
            self.close()


def _resume(pid: int) -> None:
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.restype = ctypes.c_long
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    k = _kernel32()
    process = k.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        status = ntdll.NtResumeProcess(process)
        if status != 0:
            raise OSError(f"NtResumeProcess: 0x{status & 0xFFFFFFFF:08X}")
    finally:
        k.CloseHandle(process)


@dataclass
class RunResult:
    returncode: int | None
    timed_out: bool = False
    leftover: bool = False  # после развязки в задании оставались процессы


def run(
    args: Sequence[str],
    *,
    timeout: float,
    poll: Callable[[], None] | None = None,
    cwd: os.PathLike | str | None = None,
    env: dict[str, str] | None = None,
    memory_limit: int | None = None,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    on_started: Callable[[JobObject, subprocess.Popen], None] | None = None,
) -> RunResult:
    """Запускает `args` (без оболочки) в новом объекте задания и ждёт.

    `poll()` вызывается каждые `POLL_INTERVAL` с; исключение из него
    (например, `Cancelled`) убивает всё дерево процессов и пробрасывается
    дальше. Таймаут → дерево убито, `RunResult(timed_out=True)`. В любом
    случае по выходе из функции процессов задания не остаётся (или это
    записано в журнал и `leftover=True`).
    """
    job = JobObject(memory_limit)
    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 — список аргументов, без оболочки
            list(args),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=_CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        try:
            job.assign(process.pid)
        except OSError:
            process.kill()
            raise
        _resume(process.pid)
        if on_started is not None:
            on_started(job, process)
        deadline = time.monotonic() + timeout
        while True:
            try:
                code = process.wait(POLL_INTERVAL)
                break
            except subprocess.TimeoutExpired:
                pass
            if poll is not None:
                poll()
            if time.monotonic() >= deadline:
                clean = job.terminate()
                process.wait(5)
                return RunResult(None, timed_out=True, leftover=not clean)
        clean = job.terminate()
        return RunResult(code, leftover=not clean)
    finally:
        try:
            job.terminate()
        finally:
            job.close()
        if process is not None and process.poll() is None:
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                log.warning("Процесс %s не завершился после закрытия задания", process.pid)
