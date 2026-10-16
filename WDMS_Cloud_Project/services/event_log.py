"""JSONL diagnostics with bounded retries; contains no input or query policy."""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from errors import NativeTextReadError, WDMSError
from services.file_retry import retry_permission

LOG_WRITE_RETRY_SECONDS = 1.0


def record(path: Path, event: str, **data) -> None:
    line = json.dumps({"time": datetime.now().astimezone().isoformat(timespec="milliseconds"), "event": event, **data}, ensure_ascii=False) + "\n"
    # Windows 下杀毒软件或索引服务会短暂独占刚写入的文件，使追加打开以
    # PermissionError（共享冲突）失败。这里只对这类瞬时冲突做有界重试，
    # 不改变任何业务判定；超过重试窗口仍失败则按原样抛出。
    def append():
        with path.open("a", encoding="utf-8") as output:
            output.write(line)
            output.flush()
    retry_permission(append, seconds=LOG_WRITE_RETRY_SECONDS)



def error_info(error: Exception) -> dict:
    if isinstance(error, NativeTextReadError):
        return {"error_code": error.code, "error": error.message,
                "winerror": error.winerror, "hwnd": error.hwnd,
                "class_name": error.class_name}
    if isinstance(error, WDMSError):
        return {"error_code": error.code, "error": error.message}
    kind = type(error).__name__
    info = {"error_code": "PROBE_API_FAILED", "exception_type": kind,
            "winerror": getattr(error, "winerror", None)}
    # Report code location without serializing exception args, frame locals,
    # or complete paths, which can contain credentials or business data.
    trace = error.__traceback__
    if trace is not None:
        while trace.tb_next is not None:
            trace = trace.tb_next
        filename = Path(trace.tb_frame.f_code.co_filename).name
        info.update(error_file=filename, error_line=trace.tb_lineno,
                    error_function=trace.tb_frame.f_code.co_name)
        location = f"，位置：{filename}:{trace.tb_lineno}"
    else:
        location = ""
    info["error"] = f"程序内部异常（{kind}）{location}。本轮已停止，请查看日志。"
    return info



def read_events(log: Path) -> list:
    events = []
    for number, line in enumerate(log.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or "event" not in item:
                raise ValueError("invalid event")
            events.append(item)
        except (ValueError, TypeError):
            # A killed child can leave its final write incomplete. Keep the
            # original evidence and report the rest rather than crash parsing.
            events.append({"event": "incomplete_log_record", "line": number})
    return events
