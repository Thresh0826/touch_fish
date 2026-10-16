"""Replacement for the unreviewed legacy probe. Win32-only by default."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_wdms_window import main


if __name__ == "__main__":
    raise SystemExit(main())
