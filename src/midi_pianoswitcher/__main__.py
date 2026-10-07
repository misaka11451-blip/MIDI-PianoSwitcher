"""让 `python -m midi_pianoswitcher` 等价于 `mps` 命令。"""
from __future__ import annotations

import sys

from .runner import main

if __name__ == "__main__":
    sys.exit(main())
