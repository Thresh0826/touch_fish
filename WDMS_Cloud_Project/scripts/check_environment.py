"""Read-only environment check, usable before config.json is ready."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.windows_environment import inspect_environment, require_usable_environment
from errors import WDMSError


def main() -> int:
    info = {}
    try:
        info = inspect_environment()
        require_usable_environment(info)
    except WDMSError as error:
        print(json.dumps({"environment": info, "error_code": error.code, "error": error.message}, ensure_ascii=False))
        return 2
    print(json.dumps({"environment": info, "error_code": None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
