"""量化 audio2midi 的转录准确度（拿已知真值的 MIDI 做对照）。

做法：有一份 MIDI → 渲染成音频 → 再转回 MIDI → 和原始 MIDI 对齐比较。
这等于"上限测试"（音频是干净合成的，没有真实录音的混响/噪声/母带处理）。

指标用的是 **MIREX 风格的 note-level 匹配**：一个转录音要算命中，
音高必须相同，且起音时刻落在真值音符的容差窗内。

用法：

    python -m midi_pianoswitcher.eval_audio2midi --midi 真值.mid --audio 音频.wav
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def midi_note_list(path: Path) -> list[tuple[float, int, float]]:
    """返回 [(onset_sec, pitch, dur_sec)]"""
    import pretty_midi
    pm = pretty_midi.PrettyMIDI(str(path))
    out = []
    for ins in pm.instruments:
        for n in ins.notes:
            out.append((n.start, n.pitch, n.end - n.start))
    out.sort()
    return out


def match_notes(truth: list, pred: list, onset_tol: float = 0.10):
    """贪心一对一匹配。返回 (命中, 误报, 漏检)。"""
    used = [False] * len(pred)
    tp = 0
    for t_on, t_p, _ in truth:
        best, best_d = -1, 1e9
        for j, (p_on, p_p, _) in enumerate(pred):
            if used[j] or p_p != t_p:
                continue
            d = abs(p_on - t_on)
            if d <= onset_tol and d < best_d:
                best, best_d = j, d
        if best >= 0:
            used[best] = True
            tp += 1
    fp = len(pred) - tp
    fn = len(truth) - tp
    return tp, fp, fn


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="评估 audio2midi 的转录准确度")
    ap.add_argument("--midi", required=True, help="真值 MIDI")
    ap.add_argument("--audio", required=True, help="用于转录的音频（由上面的 MIDI 渲染而来）")
    ap.add_argument("--tol", type=float, default=0.10, help="起音容差（秒）")
    ap.add_argument("--thresh", type=float, default=0.10)
    ap.add_argument("--min-len", type=float, default=0.06)
    ap.add_argument("--max-poly", type=int, default=6)
    ap.add_argument("--save-pred", default=None, help="把转录结果也存一份")
    a = ap.parse_args(argv)

    from .audio2midi import transcribe

    src_midi = Path(a.midi).expanduser()
    src_audio = Path(a.audio).expanduser()
    out_midi = Path(a.save_pred).expanduser() if a.save_pred else src_audio.with_suffix(".pred.mid")

    transcribe(src_audio, out_midi, thresh=a.thresh, min_len=a.min_len,
               max_poly=a.max_poly, verbose=False)

    truth = midi_note_list(src_midi)
    pred = midi_note_list(out_midi)

    print("=" * 68)
    print("  audio2midi 准确度评估（自己渲染的音频 → 转回 MIDI → 对真值）")
    print("=" * 68)
    print(f"  真值音符 {len(truth)}   转录音符 {len(pred)}   起音容差 ±{a.tol}s")
    print()
    print(f"  {'容差':>8} {'命中':>7} {'误报':>7} {'漏检':>7} {'精确率':>9} {'召回率':>9} {'F1':>7}")
    print("  " + "-" * 62)
    rows = []
    for tol in (0.05, a.tol, 0.15, 0.25, 0.50):
        tp, fp, fn = match_notes(truth, pred, onset_tol=tol)
        p, r, f = prf(tp, fp, fn)
        mark = " ←" if abs(tol - a.tol) < 1e-9 else ""
        print(f"  {tol:>8.2f} {tp:>7} {fp:>7} {fn:>7} {p * 100:>8.1f}% {r * 100:>8.1f}% {f:>6.2f}{mark}")
        rows.append((tol, p, r, f))

    # 同时发声密度：直接决定转录难度
    from collections import Counter
    on_sec = np.array([t[0] for t in truth])
    poly_hist = Counter()
    for i, t in enumerate(on_sec):
        n = int(((on_sec >= t - 0.02) & (on_sec <= t + 0.02)).sum())
        poly_hist[n] += 1
    maxpoly = max(poly_hist) if poly_hist else 0
    print()
    print(f"  真值的最大同时发声音符数：{maxpoly}")
    for k in sorted(poly_hist):
        print(f"    同时 {k} 个音起音：{poly_hist[k]} 次")

    f1 = rows[1][3]
    print()
    if f1 >= 0.7:
        verdict = "很好 —— 这段素材可以直接用"
    elif f1 >= 0.4:
        verdict = "一般 —— 能当草稿，需要人工大修"
    else:
        verdict = "很差 —— 建议改用原曲 MIDI，不要从音频转"
    print(f"  结论（F1={f1:.2f}）：{verdict}")
    print()
    print("  注：这是**上限测试**——音频由 MIDI 干净渲染，没有混响、噪声、")
    print("      母带压缩、多乐器混音。真实歌曲 mp3 的结果只会更差。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
