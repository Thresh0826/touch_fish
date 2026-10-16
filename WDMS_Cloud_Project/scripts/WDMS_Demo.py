"""Source and frozen entry; worker routes never create a GUI."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
if __name__=='__main__':
    if '--native-owners' in sys.argv:
        from desktop_demo.owner_discovery import main
        raise SystemExit(main(sys.argv[sys.argv.index('--native-owners')+1:]))
    if '--menu-diagnostic' in sys.argv:
        from scripts.diagnose_wdms_menu import main
        raise SystemExit(main(sys.argv[sys.argv.index('--menu-diagnostic')+1:]))
    if '--native-workflow' in sys.argv:
        from desktop_demo.workflow import main
        raise SystemExit(main(sys.argv[sys.argv.index('--native-workflow')+1:]))
    if '--native-navigate' in sys.argv:
        from desktop_demo.navigation import main
        raise SystemExit(main(sys.argv[sys.argv.index('--native-navigate')+1:]))
    if '--native-login' in sys.argv:
        from desktop_demo.login import main
        raise SystemExit(main(sys.argv[sys.argv.index('--native-login')+1:]))
    if '--native-trial' in sys.argv:
        from scripts.run_native_trial import main
        raise SystemExit(main(sys.argv[sys.argv.index('--native-trial')+1:]))
    from desktop_demo.app import main
    main()
