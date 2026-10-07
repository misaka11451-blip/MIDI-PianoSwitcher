"""批量转换：把多个 MIDI（或一个目录）挨个跑一遍。

用法：

    midi-pianoswitcher-batch a.mid b.mid --outdir out
    midi-pianoswitcher-batch --indir "D:/midi" --outdir out --audio-format flac
    midi-pianoswitcher-batch a.mid --outdir out --pdf

设计上故意**不并行**：单次渲染会吃掉几百 MB 内存和临时磁盘，
串行跑比并行稳，也更不容易把机器拖垮。
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from .runner import main as run_one

MIDI_EXT = (".mid", ".midi", ".kar", ".rmi")


def collect(paths: list[str], indir: str | None, recursive: bool) -> list[Path]:
    out: list[Path] = []
    if indir:
        d = Path(indir).expanduser()
        if not d.is_dir():
            raise SystemExit(f"--indir 不是目录：{d}")
        it = d.rglob("*") if recursive else d.glob("*")
        out += sorted(p for p in it if p.suffix.lower() in MIDI_EXT)
    for s in paths:
        p = Path(s).expanduser()
        if p.is_dir():
            out += sorted(q for q in p.glob("*") if q.suffix.lower() in MIDI_EXT)
        elif p.is_file():
            out.append(p)
        else:
            print(f"  [跳过] 找不到：{p}")
    # 去重并保持顺序
    seen, uniq = set(), []
    for p in out:
        k = str(p.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="MIDI-PianoSwitcher 批量模式：多个 MIDI 挨个转成纯钢琴版")
    ap.add_argument("paths", nargs="*", help="MIDI 文件或目录（可多个）")
    ap.add_argument("--indir", default=None, help="从目录里找 MIDI")
    ap.add_argument("--recursive", action="store_true", help="--indir 递归子目录")
    ap.add_argument("--outdir", default="mps_out")
    ap.add_argument("--pdf", action="store_true")
    ap.add_argument("--audio-format", default="wav,mp3")
    ap.add_argument("--bitdepth", type=int, default=16, choices=(16, 24))
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--continue-on-error", action="store_true", default=True,
                    help="单个失败也继续（默认开）")
    a = ap.parse_args(argv)

    files = collect(a.paths, a.indir, a.recursive)
    if not files:
        print("没有找到任何 MIDI 文件。")
        print(f"  支持的后缀：{', '.join(MIDI_EXT)}")
        return 2

    print(f"\n共 {len(files)} 个文件，开始串行处理\n")
    ok = fail = 0
    failures: list[tuple[str, str]] = []
    for i, f in enumerate(files, 1):
        print("=" * 72)
        print(f"  [{i}/{len(files)}] {f.name}")
        print("=" * 72)
        argv_one = ["--src", str(f), "--outdir", a.outdir]
        if a.pdf:
            argv_one.append("--pdf")
        argv_one += ["--audio-format", a.audio_format,
                     "--bitdepth", str(a.bitdepth)]
        if a.no_audio:
            argv_one.append("--no-audio")
        try:
            rc = run_one(argv_one)
            if rc == 0:
                ok += 1
            else:
                fail += 1
                failures.append((f.name, f"返回码 {rc}"))
        except SystemExit as e:
            fail += 1
            failures.append((f.name, f"SystemExit({e.code})"))
        except Exception as e:
            fail += 1
            failures.append((f.name, f"{type(e).__name__}: {e}"))
            if not a.continue_on_error:
                raise
            traceback.print_exc()

    print()
    print("=" * 72)
    print(f"  批量完成：成功 {ok}   失败 {fail}")
    if failures:
        print("  失败清单：")
        for name, why in failures:
            print(f"    ✗ {name}  —— {why}")
    print(f"  输出目录：{Path(a.outdir).resolve()}")
    print("=" * 72)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
