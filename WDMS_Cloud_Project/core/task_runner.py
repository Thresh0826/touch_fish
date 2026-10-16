"""Locked task lifecycle for mock runs and Windows-only real runs."""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from adapters.base import Owner
from adapters.mock_wdms import MOCK_RECORDS, MockWDMSAdapter
from config import load_config, validate_common_config, validate_real_config
from core.inventory_service import InventoryService
from errors import GracefulStop, WDMSError
from services.excel_reporter import ExcelReporter
from services.status_manager import StatusManager, atomic_json_write, local_now, read_status
from services.task_lock import TaskLock


class TaskRunner:
    def __init__(self, project_root: Path, config_path: Optional[Path] = None, output_dir: Optional[Path] = None):
        self.project_root = Path(project_root).resolve()
        self.config_path = Path(config_path).expanduser() if config_path else None
        self.output_dir_override = Path(output_dir).expanduser().resolve() if output_dir else None
        self.state_root = self.project_root / "state"

    def run(self, mode: str, limit: Optional[int] = None) -> Tuple[Path, Path]:
        if mode not in ("mock", "real"):
            raise WDMSError("INVALID_MODE", "运行模式只能是 mock 或 real。")
        if mode == "mock" and limit is not None:
            raise WDMSError("INVALID_SCOPE", "Mock 模式只运行完整的 24 个模拟货主。")
        if limit is not None and (mode != "real" or not 1 <= limit <= 23):
            raise WDMSError("INVALID_SCOPE", "Real 试验数量必须为 1 至 23；24 个货主全量运行请省略 --limit。")
        scope = "mock" if mode == "mock" else ("trial" if limit is not None else "full")
        expected_count = limit if limit is not None else 24
        lock = TaskLock(self.state_root / "task.lock")
        lock.acquire()
        manager = StatusManager(self.state_root, mode)
        adapter = None
        try:
            self._archive_interrupted(mode)
            manager.start(scope, expected_count, 24)
            config = load_config(self.config_path)
            validate_common_config(config)
            if mode == "mock":
                adapter = MockWDMSAdapter(config.get("mock_faults", {}))
                expected_owners = [Owner(name, name) for name, _, _ in MOCK_RECORDS]
            else:
                if sys.platform != "win32":
                    raise WDMSError("REAL_MODE_WINDOWS_ONLY", "Real 模式只能在 Windows 上执行；本次未连接 WDMS。")
                from adapters.windows_environment import inspect_environment, require_usable_environment

                require_usable_environment(inspect_environment())
                validate_real_config(config)
                from adapters.real_wdms import RealWDMSAdapter

                adapter = RealWDMSAdapter(config, self.project_root)
                adapter.set_task_deadline(time.monotonic() + float(config.get("timing", {}).get("total_timeout_seconds", 1800)))
                expected_owners = [Owner(str(item.get("id") or item["name"]), str(item["name"])) for item in config["owners"]]

            service = InventoryService(adapter, float(config.get("timing", {}).get("query_timeout_seconds", 15)))
            records = service.collect(
                expected_owners,
                limit=limit,
                progress=lambda done, total: manager.progress(done),
            )
            close_error = None
            try:
                adapter.close()
            except Exception as error:
                close_error = error
            finally:
                adapter = None
            if close_error:
                raise WDMSError("ADAPTER_CLOSE_FAILED", "自动化工作进程未能正常关闭，任务按失败处理。") from close_error

            started_date = date.fromisoformat(manager.value["date"])
            output_root = self._output_root(config)
            if mode == "mock":
                destination_dir = output_root / "mock"
            elif limit is not None:
                destination_dir = output_root / "real-trial"
            elif self.output_dir_override is None and config.get("real_output_dir"):
                configured = Path(str(config["real_output_dir"])).expanduser()
                destination_dir = configured.resolve() if configured.is_absolute() else (self.project_root / configured).resolve()
            else:
                destination_dir = output_root / "real"
            output_file, digest = ExcelReporter().publish(
                destination_dir,
                started_date,
                mode,
                scope,
                manager.value["run_id"],
                expected_count,
                24,
                records,
            )
            manager.complete(output_file, digest)
            return output_file, manager.path
        except (Exception, KeyboardInterrupt) as error:
            failure = GracefulStop("KeyboardInterrupt") if isinstance(error, KeyboardInterrupt) else error
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    # Adapter cleanup is secondary; persist and raise the original task failure below.
                    pass
            try:
                manager.fail(failure)
            except Exception as status_error:
                raise WDMSError("STATUS_WRITE_FAILED", "任务失败，且失败状态无法写入；不得认定本次报表成功。") from status_error
            if failure is not error:
                raise failure from error
            raise
        finally:
            lock.release()

    def _output_root(self, config: Dict[str, Any]) -> Path:
        if self.output_dir_override is not None:
            return self.output_dir_override
        configured = Path(str(config.get("output_root", "output"))).expanduser()
        return configured.resolve() if configured.is_absolute() else (self.project_root / configured).resolve()

    def _archive_interrupted(self, mode: str) -> None:
        status_path = self.state_root / mode / "status.json"
        try:
            previous = read_status(status_path)
        except WDMSError:
            previous = None
        if not previous or previous.get("status") != "running":
            return
        now = local_now()
        previous.update(
            {
                "status": "failed",
                "completion_time": now.isoformat(timespec="seconds"),
                "heartbeat_time": now.isoformat(timespec="seconds"),
                "output_file": None,
                "sha256": None,
                "error_code": "PREVIOUS_RUN_INTERRUPTED",
                "error": "上一次任务未能提交最终状态，结果不可确认。",
            }
        )
        archive_dir = status_path.parent / "history"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"{previous.get('run_id', 'unknown')}.json"
        atomic_json_write(archive_path, previous)
        atomic_json_write(status_path, previous)
