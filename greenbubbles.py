"""Console entry point for GreenBubbles for Windows.

Works from a checkout (``python greenbubbles.py ...``) and after
``pip install -e .``, where ``greenbubbles`` resolves to the same ``main``.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

from greenbubbles_win.cli import main

if __name__ == "__main__":
    sys.exit(main())
