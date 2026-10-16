"""WDMS inventory automation command line entry point."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from typing import Optional, Sequence

from core.task_runner import TaskRunner
from errors import GracefulStop, TaskAlreadyRunning, WDMSError


PROJECT_ROOT = Path(__file__).resolve().parent


def _handle_signal(signum, frame):
    name = "SIGINT" if signum == getattr(signal, "SIGINT", None) else "SIGTERM"
    raise GracefulStop(name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地 WDMS 库存采集与报表程序")
    parser.add_argument("--mode", choices=("mock", "real"), default="mock", help="运行模式，默认 mock")
    parser.add_argument("--limit", type=int, help="Real 试验采集数量（1–23）；不设置表示 24 个货主全量")
    parser.add_argument("--config", type=Path, help="配置文件路径")
    parser.add_argument("--output-dir", type=Path, help="覆盖报表根目录，Mock/Real 子目录仍分开")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)
    runner = TaskRunner(PROJECT_ROOT, args.config, args.output_dir)
    try:
        output_file, status_path = runner.run(args.mode, args.limit)
    except TaskAlreadyRunning as error:
        print(f"[{error.code}] {error.message}", file=sys.stderr)
        return 3
    except GracefulStop as error:
        print(f"[{error.code}] {error.message}", file=sys.stderr)
        return 2
    except WDMSError as error:
        print(f"[{error.code}] {error.message}", file=sys.stderr)
        return 2
    except Exception:
        print("[UNEXPECTED_ERROR] 任务失败，未能确认本次报表结果。", file=sys.stderr)
        return 2
    if args.mode == "mock":
        print("模拟库存报表已生成，结果仅供测试。")
    elif args.limit is not None:
        print("Real 试验报表已生成，等待现场核验。")
    else:
        print("今日库存分析报表已经生成，等待人工核验。")
    print(f"报表：{output_file}")
    print(f"状态：{status_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
