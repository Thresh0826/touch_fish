"""Atomic, data-minimized task status persistence."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from errors import WDMSError


def local_now() -> datetime:
    return datetime.now().astimezone()


def atomic_json_write(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some Windows/filesystem combinations cannot fsync directories; file fsync and replace already completed.
            pass
    except OSError as error:
        try:
            os.unlink(temp_name)
        except OSError:
            # Temporary-file cleanup must preserve the original STATUS_WRITE_FAILED cause.
            pass
        raise WDMSError("STATUS_WRITE_FAILED", "本地状态文件写入失败。") from error


def read_status(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise WDMSError("STATUS_INVALID", "状态文件无法读取或格式无效。") from error
    if not isinstance(value, dict):
        raise WDMSError("STATUS_INVALID", "状态文件结构无效。")
    return value


class StatusManager:
    def __init__(self, state_root: Path, mode: str):
        if mode not in ("mock", "real"):
            raise ValueError("unsupported mode")
        self.path = Path(state_root) / mode / "status.json"
        self.mode = mode
        self.value: Dict[str, Any] = {}

    def start(self, scope: str, expected_count: int, total_owner_count: int) -> str:
        now = local_now()
        self.value = {
            "run_id": str(uuid.uuid4()),
            "date": now.date().isoformat(),
            "mode": self.mode,
            "scope": scope,
            "status": "running",
            "start_time": now.isoformat(timespec="seconds"),
            "completion_time": None,
            "heartbeat_time": now.isoformat(timespec="seconds"),
            "parent_pid": os.getpid(),
            "completed_count": 0,
            "expected_count": expected_count,
            "total_owner_count": total_owner_count,
            "output_file": None,
            "sha256": None,
            "error_code": None,
            "error": None,
        }
        self._commit()
        return self.value["run_id"]

    def progress(self, completed_count: int) -> None:
        if self.value.get("status") != "running":
            raise WDMSError("STATUS_TRANSITION_INVALID", "任务状态不允许更新进度。")
        self.value["completed_count"] = completed_count
        self.value["heartbeat_time"] = local_now().isoformat(timespec="seconds")
        self._commit()

    def complete(self, output_file: Path, sha256: str) -> None:
        if self.value.get("status") != "running":
            raise WDMSError("STATUS_TRANSITION_INVALID", "任务状态不允许标记完成。")
        now = local_now()
        self.value.update(
            {
                "status": "completed_pending_review",
                "completion_time": now.isoformat(timespec="seconds"),
                "heartbeat_time": now.isoformat(timespec="seconds"),
                "output_file": str(Path(output_file).resolve()),
                "sha256": sha256,
                "error_code": None,
                "error": None,
            }
        )
        self._commit()

    def fail(self, error: Exception) -> None:
        code = error.code if isinstance(error, WDMSError) else "UNEXPECTED_ERROR"
        message = error.message if isinstance(error, WDMSError) else "任务遇到未分类错误，未发布本次报表。"
        now = local_now()
        if not self.value:
            self.value = {
                "run_id": str(uuid.uuid4()),
                "date": now.date().isoformat(),
                "mode": self.mode,
                "scope": "unknown",
                "status": "running",
                "start_time": now.isoformat(timespec="seconds"),
                "completion_time": None,
                "heartbeat_time": now.isoformat(timespec="seconds"),
                "parent_pid": os.getpid(),
                "completed_count": 0,
                "expected_count": 24,
                "total_owner_count": 24,
            }
        self.value.update(
            {
                "status": "failed",
                "completion_time": now.isoformat(timespec="seconds"),
                "heartbeat_time": now.isoformat(timespec="seconds"),
                "output_file": None,
                "sha256": None,
                "error_code": code,
                "error": message,
            }
        )
        self._commit()

    def _commit(self) -> None:
        atomic_json_write(self.path, self.value)
