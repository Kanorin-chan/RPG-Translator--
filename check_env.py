# -*- coding: utf-8 -*-
"""Start-up check run by run-ui.bat / run-server.bat. Stdlib only.
Exit 0 = good to launch. Exit 1 = something the user must fix (message printed)."""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MIN = (3, 10)


def main(want_ui=True):
    if sys.version_info < MIN:
        print("[x] Python %d.%d is too old. Install Python %d.%d or newer from https://www.python.org/downloads/"
              % (sys.version_info[0], sys.version_info[1], MIN[0], MIN[1]))
        print("    (tick 'Add python.exe to PATH' in the installer)")
        return 1
    print("[ok] Python %d.%d.%d" % sys.version_info[:3])

    if want_ui:
        try:
            import tkinter  # noqa: F401
            print("[ok] tkinter")
        except ImportError:
            print("[x] tkinter is missing. Re-run the Python installer, choose Modify and tick 'tcl/tk and IDLE'.")
            return 1
        try:
            import tkinterdnd2  # noqa: F401
            print("[ok] tkinterdnd2 (drag and drop)")
        except ImportError:
            print("[--] tkinterdnd2 not installed: drag and drop is off, the Browse buttons still work.")
            print("     To enable it:  %s -m pip install tkinterdnd2" % os.path.basename(sys.executable or "python"))

    cfg, example = os.path.join(HERE, "config.json"), os.path.join(HERE, "config.example.json")
    if not os.path.exists(cfg) and os.path.exists(example):
        shutil.copyfile(example, cfg)
        print("[ok] created config.json from config.example.json")
    elif os.path.exists(cfg):
        print("[ok] config.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(want_ui="--server" not in sys.argv))
