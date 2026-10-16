"""Versioned task journal; atomic publication precedes every external action."""
from __future__ import annotations

import copy
import json
import math
import re
import uuid
from datetime import datetime
from pathlib import Path

from errors import WDMSError
from moyu_tools.model import digest, fail, zip_name
from services.status_manager import atomic_json_write

SCHEMA = 2
PHASES = {"pending", "query_intent", "querying", "queried", "searching", "selected",
          "copy_intent", "copying", "verified", "done", "empty"}


def valid_rows(data):
    for row in data["segments"]:
        phase = row["phase"]
        if phase not in PHASES:
            return False
        if phase in ("query_intent", "querying"):
            timestamp = row["submitted_wall"]
            if type(row["busy_seen"]) is not bool or type(timestamp) not in (int, float) or not math.isfinite(timestamp):
                return False
            waited = row.get("initial_wait_seconds", 0)
            if type(waited) not in (int, float) or not math.isfinite(waited) or waited < 0:
                return False
        if phase in ("queried", "searching", "selected", "copy_intent", "copying", "verified", "done"):
            if type(row["count"]) is not int or row["count"] not in (0, 1):
                return False
            if row["count"] == 0:
                if phase != "queried" or row.get("number") is not None:
                    return False
            elif not isinstance(row.get("number"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", row["number"]):
                return False
        if phase in ("selected", "copy_intent", "copying"):
            selected = row["selected"]
            zip_name(selected["name"])
            if type(selected["sequence"]) is not int:
                return False
            if selected["size"] is not None and (type(selected["size"]) is not int or selected["size"] < 0):
                return False
        if data["identity"].get("copy_mode") == "direct":
            if phase == "verified" or phase in ("copy_intent", "copying") and row.get("destination_absent") is not True:
                return False
        if phase in ("verified", "done"):
            receipt = row["file"]
            zip_name(receipt["name"])
            if type(receipt["size"]) is not int or receipt["size"] < 0 or not re.fullmatch(r"[a-f0-9]{64}", receipt["sha256"]):
                return False
        if phase == "empty" and row.get("count") != 0:
            return False
    binding = data["identity"]["binding"]
    return not data["trial_passed"] or any(row["phase"] == "done" and row.get("query_binding") == binding
                                           and row.get("source_binding") == binding for row in data["segments"])


class Store:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.path = self.folder / "progress.json"
        self.value = None

    def load_or_create(self, plan, output, binding_digest, condition_signature=None, copy_mode=None):
        expected = {"plan": plan.document(), "output": str(Path(output).resolve()),
                    "binding": binding_digest, "conditions": condition_signature}
        if copy_mode == "direct":
            expected["copy_mode"] = "direct"
        elif copy_mode is not None:
            fail("MOYU_PROGRESS_MISMATCH", "未知的复制方式，未更改旧任务。")
        if self.path.exists():
            if self.path.is_symlink() or self.path.stat().st_size > 32 * 1024 * 1024:
                fail("MOYU_PROGRESS_INVALID", "进度文件不是有效的本地任务记录。")
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                valid = (type(data["schema"]) is int and data["schema"] == SCHEMA and data["identity"] == expected
                         and data["identity_sha256"] == digest(expected)
                         and isinstance(data["trial_passed"], bool)
                         and uuid.UUID(data["run_id"]).hex == data["run_id"]
                         and [x["key"] for x in data["segments"]] == [s.key for s in plan.segments()]
                         and all(all(row.get(k) == v for k, v in source_fields(segment).items())
                                 for row, segment in zip(data["segments"], plan.segments()))
                         and valid_rows(data))
            except (ValueError, KeyError, TypeError, OSError, WDMSError):
                valid = False
            if not valid:
                fail("MOYU_PROGRESS_MISMATCH", "进度损坏或表格任务、文件夹、定位配置已变化；请保留旧任务并新建任务。")
            self.value = data
        else:
            self.value = {"schema": SCHEMA, "run_id": uuid.uuid4().hex,
                          "identity": expected, "identity_sha256": digest(expected),
                          "trial_passed": False, "status": "ready", "error": None,
                          "segments": [{"key": s.key, "phase": "pending", **source_fields(s)} for s in plan.segments()]}
            self.commit()
        return self.value

    def commit(self):
        self.value["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
        atomic_json_write(self.path, self.value)

    def update(self, index, phase, **fields):
        if phase not in PHASES:
            fail("MOYU_PROGRESS_INVALID", "未知任务阶段。")
        previous = copy.deepcopy(self.value)
        try:
            self.value["segments"][index].update(fields, phase=phase)
            self.commit()
        except BaseException:
            self.value = previous
            raise

    def stage_folder(self, index):
        return (Path(self.value["identity"]["output"]) / ".moyu-staging" /
                self.value["run_id"] / self.value["segments"][index]["key"])

    def pause(self, error):
        self.value.update(status="paused", error={"code": error.code, "message": error.message})
        self.commit()

    def rebind(self, binding_digest):
        old = self.value["identity"]["binding"]
        self.value.setdefault("binding_history", []).append({"previous": old, "current": binding_digest})
        self.value["identity"]["binding"] = binding_digest
        self.value["identity_sha256"] = digest(self.value["identity"])
        self.value.update(trial_passed=False, status="ready", error=None)
        self.commit()

    def receipt(self, number):
        return next((row["file"] for row in self.value["segments"]
                     if row["phase"] == "done" and row.get("number") == number), None)

    def file_receipt(self, name):
        return next((row["file"] for row in self.value["segments"]
                     if row["phase"] == "done" and row["file"]["name"].casefold() == name.casefold()), None)


def source_fields(segment):
    return ({"source_row": segment.source_row, "query_date": segment.start.isoformat(), "contract": segment.contract}
            if hasattr(segment, "contract") else {})
