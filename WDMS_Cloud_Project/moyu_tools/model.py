"""Business settings and inclusive calendar partitions, independent of GUI/Win32."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path, PureWindowsPath

from errors import WDMSError


def fail(code, message):
    raise WDMSError(code, message)


@dataclass(frozen=True)
class Policy:
    initial_wait: float = 30
    settle: float = 5
    query_timeout: float = 600
    search_timeout: float = 60
    copy_timeout: float = 300
    poll: float = 0.1
    file_settle: float = 3
    preparation: float = 45

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                fail("MOYU_POLICY_INVALID", "等待设置必须是有限数字。")
            if value < 0 or (name != "initial_wait" and value == 0):
                fail("MOYU_POLICY_INVALID", "等待设置必须为正数，初始等待可为零。")
        if self.query_timeout <= self.initial_wait + self.settle or self.poll > self.settle:
            fail("MOYU_POLICY_INVALID", "查询上限必须超过初始等待加稳定观察时间。")

    def budget(self, count):
        return count * (self.preparation + self.query_timeout + self.search_timeout + self.copy_timeout) + 30


@dataclass(frozen=True)
class Segment:
    start: date
    end: date

    @property
    def key(self):
        return self.start.isoformat() + "_" + self.end.isoformat()


@dataclass(frozen=True)
class Plan:
    start: date = date(2023, 11, 1)
    end: date = date(2026, 1, 31)
    days: int = 7

    def __post_init__(self):
        if type(self.start) is not date or type(self.end) is not date or self.start > self.end:
            fail("MOYU_RANGE_INVALID", "请填写有效的起止日期，开始日期不能晚于结束日期。")
        if type(self.days) is not int or not 1 <= self.days <= 366:
            fail("MOYU_RANGE_INVALID", "每段天数须为1至366的整数。")
        if (self.end - self.start).days > 36525:
            fail("MOYU_RANGE_INVALID", "日期范围过大，请分成多个任务。")

    def segments(self):
        current = self.start
        while current <= self.end:
            end = min(self.end, current + timedelta(days=min(self.days - 1, (date.max-current).days)))
            yield Segment(current, end)
            if end == self.end:
                break
            current = end + timedelta(days=1)

    def document(self):
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "days": self.days}

    @classmethod
    def parse(cls, data):
        if data.get("kind") == "sheet_rows":
            from moyu_tools.sheet import SheetPlan
            return SheetPlan.parse(data)
        if "kind" in data:
            fail("MOYU_PLAN_INVALID", "任务类型无法识别，请保留旧任务并重新导入表格。")
        return cls(date.fromisoformat(data["start"]), date.fromisoformat(data["end"]), data["days"])


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def zip_name(value):
    if (not isinstance(value, str) or not value or len(value) > 240
            or value in (".", "..") or PureWindowsPath(value).name != value
            or any(c in value for c in '<>:"/\\|?*') or any(ord(c) < 32 for c in value)
            or value.endswith((" ", ".")) or Path(value).suffix.lower() != ".zip"
            or value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL"} |
            {"COM"+str(i) for i in range(1, 10)} | {"LPT"+str(i) for i in range(1, 10)}):
        fail("MOYU_FILE_NAME_INVALID", "剪贴板文件名不是安全的单个ZIP文件名。")
    return value
