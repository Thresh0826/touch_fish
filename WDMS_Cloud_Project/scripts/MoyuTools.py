"""Independent source entry: worker routes cannot create the Tk window."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--worker":
        if len(arguments) != 2:
            raise SystemExit("--worker requires one local request file")
        from moyu_tools.runtime import worker
        return worker(arguments[1])
    if arguments:
        raise SystemExit("Unknown MoyuTools option")
    from moyu_tools.app import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
