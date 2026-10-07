"""让 `python -m pianoize` 等价于 `pianoize` 命令。"""
from __future__ import annotations

import sys

from .runner import main

if __name__ == "__main__":
    sys.exit(main())
