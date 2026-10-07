"""pianoize 的命令行入口（console_scripts: `pianoize`）。"""
from __future__ import annotations

import sys

from .runner import main

__all__ = ["main"]


def entry() -> int:
    return main()


if __name__ == "__main__":
    sys.exit(entry())
