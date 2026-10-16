"""Parent-side hard-timeout supervisor for the Windows automation worker."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, Optional, Sequence

from errors import WDMSError


class JsonLineWorker:
    """Supervise a single child process; a timed-out worker is never reused."""

    def __init__(self, command: Sequence[str], cwd: str):
        self.command = list(command)
        self.cwd = cwd
        self.process: Optional[subprocess.Popen] = None
        self._responses: "queue.Queue[Optional[str]]" = queue.Queue()
        self._request_lock = threading.Lock()
        self._failed = False

    def _ensure_started(self) -> None:
        if self._failed:
            raise WDMSError("WORKER_UNAVAILABLE", "自动化工作进程已停止，不能继续操作。")
        if self.process is not None:
            if self.process.poll() is not None:
                self._failed = True
                raise WDMSError("WORKER_EXITED", "自动化工作进程意外退出。")
            return
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as error:
            self._failed = True
            raise WDMSError("WORKER_START_FAILED", "无法启动隔离的自动化工作进程。") from error
        reader = threading.Thread(target=self._read_lines, daemon=True, name="wdms-worker-reader")
        reader.start()

    def _read_lines(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self._responses.put(line)
        finally:
            self._responses.put(None)

    def request(self, action: str, payload: Optional[Dict[str, Any]], timeout_seconds: float) -> Dict[str, Any]:
        if timeout_seconds <= 0:
            self._terminate()
            raise WDMSError("TASK_TIMEOUT", "Real 任务总时限已到，自动化工作进程已停止。")
        with self._request_lock:
            self._ensure_started()
            assert self.process is not None and self.process.stdin is not None
            request_id = str(uuid.uuid4())
            request = {"id": request_id, "action": action, "payload": payload or {}}
            try:
                message = json.dumps(request, ensure_ascii=False) + "\n"
            except (TypeError, ValueError) as error:
                raise WDMSError("WORKER_PROTOCOL_INVALID", "自动化请求内容无法编码。") from error
            if len(message.encode("utf-8")) > 65536:
                raise WDMSError("WORKER_PROTOCOL_INVALID", "自动化请求超过消息大小限制。")
            deadline = time.monotonic() + timeout_seconds
            write_done = threading.Event()
            write_error: Dict[str, BaseException] = {}

            def write_request() -> None:
                try:
                    assert self.process is not None and self.process.stdin is not None
                    self.process.stdin.write(message)
                    self.process.stdin.flush()
                except BaseException as error:
                    write_error["error"] = error
                finally:
                    write_done.set()

            writer = threading.Thread(target=write_request, daemon=True, name="wdms-worker-writer")
            writer.start()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not write_done.wait(max(0, remaining)):
                self._terminate()
                raise WDMSError("WORKER_HARD_TIMEOUT", "自动化请求写入超过硬时限，子进程已终止。")
            if "error" in write_error:
                self._terminate()
                raise WDMSError("WORKER_COMMUNICATION_FAILED", "自动化工作进程通信失败。") from write_error["error"]
            try:
                line = self._responses.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as error:
                self._terminate()
                raise WDMSError("WORKER_HARD_TIMEOUT", f"Real 自动化动作超过 {timeout_seconds:g} 秒硬时限，子进程已终止。") from error
            if line is None:
                self._terminate()
                raise WDMSError("WORKER_EXITED", "自动化工作进程在返回结果前退出。")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as error:
                self._terminate()
                raise WDMSError("WORKER_PROTOCOL_INVALID", "自动化工作进程返回了无效消息。") from error
            if response.get("id") != request_id:
                self._terminate()
                raise WDMSError("WORKER_PROTOCOL_INVALID", "自动化工作进程响应与请求不匹配。")
            if not response.get("ok"):
                error = response.get("error", {})
                raise WDMSError(str(error.get("code", "REAL_ACTION_FAILED")), str(error.get("message", "Real 自动化动作失败。")))
            result = response.get("result", {})
            return result if isinstance(result, dict) else {"value": result}

    def close(self, timeout_seconds: float = 2.0) -> None:
        if self.process is None:
            return
        if not self._failed and self.process.poll() is None:
            try:
                self.request("close", {}, timeout_seconds)
            except WDMSError:
                # The close handshake failed; hard cleanup below still terminates our child.
                pass
        self._terminate()

    def _terminate(self) -> None:
        self._failed = True
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    # After kill, a second bounded wait must not extend the primary timeout.
                    pass
            except OSError:
                # The child may exit during cleanup; preserve the original task failure.
                pass
        # Avoid buffered pipe close calls on the timeout path; process termination
        # releases the handles without allowing a blocked flush/close to extend it.
