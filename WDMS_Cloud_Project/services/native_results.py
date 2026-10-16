"""Publish and validate worker receipts independently of diagnostic JSONL."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from core.native_collection import InventoryReading, count_key
from errors import WDMSError
from services.file_retry import retry_permission


RESULT_SCHEMA = 1


def write_worker_result(job, *, reading=None, failure=None, stage=None):
    if (reading is None) == (failure is None):
        raise WDMSError("TRIAL_RESULT_INVALID", "采集凭证须包含一种明确的完成或失败状态。")
    envelope = {
        "schema_version": RESULT_SCHEMA, "worker_id": job.worker_id, "run_id": job.run_id,
        "owner_index": job.index, "owners_sha256": job.owners_sha256,
        "status": "complete" if reading is not None else "failed",
        "reading": reading.as_row() if reading is not None else None,
        "failure": failure, "stage": stage,
    }
    destination = Path(job.result)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    if destination.exists() or destination.is_symlink():
        raise WDMSError("TRIAL_RESULT_EXISTS", "本货主已有采集凭证，停止且不覆盖。")
    try:
        with retry_permission(lambda: temporary.open("x", encoding="utf-8")) as handle:
            json.dump(envelope, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        def publish():
            if destination.exists() or destination.is_symlink():
                raise WDMSError("TRIAL_RESULT_EXISTS", "发布前发现既有采集凭证，停止且不覆盖。")
            return temporary.replace(destination)
        retry_permission(publish)
    finally:
        temporary.unlink(missing_ok=True)


def read_worker_result(job):
    path = Path(job.result)
    if path.is_symlink():
        raise WDMSError("TRIAL_RESULT_INVALID", "采集凭证不能是符号链接。")
    try:
        if path.stat().st_size > 65536:
            raise ValueError("oversized receipt")
        data = json.loads(retry_permission(lambda: path.read_text(encoding="utf-8")))
        expected_keys = {"schema_version", "worker_id", "run_id", "owner_index", "owners_sha256",
                         "status", "reading", "failure", "stage"}
        if not isinstance(data, dict) or set(data) != expected_keys:
            raise ValueError("invalid envelope")
        if (type(data["schema_version"]) is not int or data["schema_version"] != RESULT_SCHEMA
                or type(data["owner_index"]) is not int or data["owner_index"] != job.index
                or data["worker_id"] != job.worker_id or data["run_id"] != job.run_id
                or data["owners_sha256"] != job.owners_sha256):
            raise ValueError("receipt belongs to a different request")
        if data["status"] == "failed":
            failure = data["failure"]
            if data["reading"] is not None or not isinstance(failure, dict):
                raise ValueError("invalid failure")
            code, message = failure.get("error_code"), failure.get("error")
            if not isinstance(code, str) or not code or not isinstance(message, str):
                raise ValueError("invalid failure reason")
            raise WDMSError(code, message)
        row = data["reading"]
        if data["status"] != "complete" or data["failure"] is not None or not isinstance(row, dict):
            raise ValueError("not a completed result")
        if (row.get("owner") != job.expected_owner or not isinstance(row.get("owner"), str)
                or row.get("requires_manual_review") is not True
                or row.get("result_owner_independently_verified") is not False):
            raise ValueError("owner or evidence flags differ")
        total = row.get("total")
        if total is not None and not isinstance(total, str):
            raise ValueError("invalid diagnostic text")
        whole, loose = count_key(row)
        return InventoryReading(row["owner"], whole, loose, total)
    except WDMSError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise WDMSError("TRIAL_RESULT_UNCONFIRMED", "未取得与本货主任务一致的完整采集凭证；停止且不重试查询。") from error
