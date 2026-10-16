"""Startup dependency check only. Does not open or inspect business windows."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    from moyu_tools.portable import check
    from moyu_tools.settings import state_root
    from services.event_log import error_info
    from services.status_manager import atomic_json_write
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-project", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        result = {"ok": True, **check(args.runtime_project)}
    except Exception as error:
        result = {"ok": False, **error_info(error)}
    atomic_json_write(state_root()/"portable_startup.json", result)
    print("Bundled runtime OK." if result["ok"] else result["error"])
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
