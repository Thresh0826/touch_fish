"""Supervise only the child we start; cancellation never retries its actions."""
from __future__ import annotations
import math
import subprocess
import time
from pathlib import Path
from services import event_log
from services.event_log import error_info

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def supervise(command: list, log: Path, timeout: float, cancelled=None, *, stderr_path=None, emit=None, cwd=None) -> int:
    record = emit or event_log.record
    try:
        valid_timeout = not isinstance(timeout, bool) and math.isfinite(timeout) and timeout > 0
    except (TypeError, ValueError, OverflowError):
        valid_timeout = False
    if not valid_timeout:
        record(log, "probe_timeout", error_code="PROBE_HARD_TIMEOUT", error="诊断硬期限必须是有限正数。")
        return 2

    duration = float(timeout)
    process = None
    stderr_file = None
    try:
        if cancelled is not None and cancelled():
            record(log, "probe_interrupted", error_code="TASK_CANCELLED")
            return 2
        # No buffered pipes: even a stuck native call cannot block a flush/close.
        if stderr_path is not None:
            stderr_file = Path(stderr_path).open('xb')
        process = subprocess.Popen(command, cwd=str(cwd or PROJECT_ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=stderr_file if stderr_file is not None else subprocess.DEVNULL)
        deadline = time.monotonic() + duration if cancelled is not None else None
        record(log, "child_started", pid=process.pid, timeout_seconds=duration)
        def finished(code):
            record(log, "child_exited", pid=process.pid, exit_code=code)
            return code
        if cancelled is None:
            return finished(process.wait(timeout=timeout))

        while True:
            if cancelled():
                record(log, "probe_interrupted", error_code="TASK_CANCELLED")
                return 2
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                record(log, "probe_timeout", error_code="PROBE_HARD_TIMEOUT", error="诊断子进程超过硬时限，仅停止诊断进程；WDMS 未被关闭或重启。")
                return 2
            try:
                return finished(process.wait(timeout=min(0.25, remaining)))
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired:
        record(log, "probe_timeout", error_code="PROBE_HARD_TIMEOUT", error="诊断子进程超过硬时限，仅停止诊断进程；WDMS 未被关闭或重启。")
        return 2
    except KeyboardInterrupt:
        record(log, "probe_interrupted", error_code="PROBE_INTERRUPTED")
        return 2
    except OSError as error:
        record(log, "probe_start_failed", **error_info(error))
        return 2
    finally:
        try:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired) as error:
                    record(log, "probe_cleanup_failed", **error_info(error))
        finally:
            if stderr_file is not None:
                stderr_file.close()

