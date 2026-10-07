"""pianoize —— 把任意 MIDI 转成纯钢琴版（音频 + MIDI + 可打印钢琴谱）。

命令行用法：

    pianoize --src song.mid --outdir out --pdf

当库用：纯逻辑都在 `pianoize.core` 里，全部是可单独调用的函数。

    from pianoize import core

    pm = core.pretty_midi.PrettyMIDI("song.mid")
    info = core.analyze(pm, bpm=120)
    print(info["duplicates"])       # 重复轨检测结果
"""
from __future__ import annotations

from . import core

__version__ = "0.2.0"
__all__ = ["core", "__version__"]
