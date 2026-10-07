#!/usr/bin/env python -u
# -*- coding: utf-8 -*-
"""构造带边界的合成测试 MIDI，专门压 pianoize 的各个分支。

这份文件是**本项目原创**，所以可以安全地随仓库分发（真实歌曲的 MIDI 不行）。

覆盖：
  * 打击乐（ch9）+ 多乐器轨 + 重复轨（自动检测应命中）
  * 同音高零间隔重击（retrigger 修复应命中）
  * 3/4 拍号（非 4/4 的分小节与校验）
  * 窄音域（左右手无法按中位数分开，应触发 choose_split 兜底）
  * 音符跨度很长（长音尾不得把小节撑爆）

注意：轨名一律用 ASCII。mido 自己就写不出中文轨名（MIDI 文本事件是
latin-1），所以"中文轨名"那条边界没法在这里造 —— 它在
`tests/test_core.py::test_chinese_track_name_survives_write` 里
用**手工拼原始字节**的方式覆盖。
"""
import sys
from pathlib import Path

import mido

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TPB = 480
BAR = TPB * 3          # 3/4：一小节 3 个四分音符


def track(name, program, channel, events):
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage("track_name", name=name, time=0))
    if program is not None:
        tr.append(mido.Message("program_change", channel=channel, program=program, time=0))
    prev = 0
    for tick, msg in sorted(events, key=lambda e: e[0]):
        msg.time = max(0, tick - prev)
        prev = tick
        tr.append(msg)
    return tr


def main(outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / "stress.mid"
    m = mido.MidiFile(type=1, ticks_per_beat=TPB)

    # --- 轨0：速度/拍号 ---
    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    meta.append(mido.MetaMessage("time_signature", numerator=3, denominator=4, time=0))
    meta.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(90), time=0))
    m.tracks.append(meta)

    # --- 轨1：钢琴主旋律（右手），含大量同音高零间隔重击 ---
    ev = []
    for i in range(24):
        t = i * (TPB // 2)
        ev.append((t, mido.Message("note_on", channel=0, note=72 + (i % 5), velocity=88)))
        ev.append((t + TPB // 2, mido.Message("note_off", channel=0, note=72 + (i % 5), velocity=0)))
    # 重击：同一音高在同一 tick 上 off 后立刻 on（间隔 0）
    for i in range(12):
        t = 24 * (TPB // 2) + i * TPB
        ev.append((t, mido.Message("note_off", channel=0, note=64, velocity=0)))
        ev.append((t, mido.Message("note_on", channel=0, note=64, velocity=80)))
        ev.append((t + TPB, mido.Message("note_off", channel=0, note=64, velocity=0)))
    # 超长音：跨越 4 个小节，长音尾最容易把后面的小节撑爆
    ev.append((0, mido.Message("note_on", channel=0, note=79, velocity=70)))
    ev.append((4 * BAR, mido.Message("note_off", channel=0, note=79, velocity=0)))
    m.tracks.append(track("Piano Lead", 0, 0, ev))

    # --- 轨2：与轨1 高度重复（相似度应 > 0.85，自动去重命中）---
    ev2 = [(t, msg.copy(channel=1)) for t, msg in ev if msg.type.startswith("note")]
    m.tracks.append(track("Vibraphone Layer", 11, 1, ev2))

    # --- 轨3：低音（左手），窄音域 40-52 ---
    ev3 = []
    for i in range(36):
        t = i * TPB
        n = 40 + (i % 4)
        ev3.append((t, mido.Message("note_on", channel=2, note=n, velocity=75)))
        ev3.append((t + TPB - 10, mido.Message("note_off", channel=2, note=n, velocity=0)))
    m.tracks.append(track("Bass Hand", 32, 2, ev3))

    # --- 轨4：打击乐 ch9（默认应静音）---
    ev4 = []
    for i in range(36):
        t = i * TPB
        ev4.append((t, mido.Message("note_on", channel=9, note=36 if i % 2 == 0 else 38, velocity=100)))
        ev4.append((t + 60, mido.Message("note_off", channel=9, note=36 if i % 2 == 0 else 38, velocity=0)))
    m.tracks.append(track("Drums", 0, 9, ev4))

    m.save(str(out))
    print(f"已生成 {out}")
    print(f"  拍号 3/4   速度 90 BPM   轨道 {len(m.tracks)} 条   时长 {m.length:.1f}s")
    print("  预期：重复轨(1 vs 2)被自动删除、鼓轨静音、重击被修、3/4 分小节、")
    print("        窄音域低音轨单独成手、轨名中文不炸")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
