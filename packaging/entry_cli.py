import sys

# Windows consoles often use a legacy code page (cp437/cp1252); never crash while printing.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

from jarvis.cli import main  # noqa: E402
from jarvis.launcher import run  # noqa: E402

raise SystemExit(run(main))
