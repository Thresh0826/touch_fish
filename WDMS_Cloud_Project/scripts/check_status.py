"""Local status query for a user or a local WorkBuddy integration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services.status_checker import StatusChecker


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 WDMS 本地任务状态，不输出库存明细。")
    parser.add_argument("--mode", required=True, choices=("mock", "real"))
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    code, result = StatusChecker(args.root).check(args.mode)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result["message"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
