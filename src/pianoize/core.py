#!/usr/bin/env python -u
# -*- coding: utf-8 -*-
"""
core —— pianoize 的纯逻辑层（诊断 / 改音色 / 修重击 / 写谱 / 校验 / 渲染 / 响度）。

这一层不碰 argparse、不碰 sys.exit，所有函数都可以单独 import 做单元测试；
命令行编排在 runner.py 里。

设计原则（都是实测踩出来的）：
  1) 先诊断再动手    —— 不看清楚就改音色，会把打击乐也变成钢琴、或漏掉通道
  2) 重复轨必须处理  —— 同一段音乐叠两遍，全改钢琴 = 响 4 LU 且发糊（实测 4.26 LU）
  3) 重击必须修      —— 同音高零间隔 note_on 会让采样器咔哒（实测 1370 处）
  4) 谱面必须逐声部校验 —— "小节被撑爆"的乐谱 MuseScore 直接拒收（exit 1320）；
                        只看小节总时值会漏报，必须按 voice 累加
  5) 左右手不能靠轨序 —— 轨序号跟上下声部无关，实测会左右反
"""
from __future__ import annotations

import math
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import numpy as np
import pretty_midi

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MUSESCORE_CANDIDATES = [
    r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
    r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe",
    r"C:\Program Files (x86)\MuseScore 4\bin\MuseScore4.exe",
]
# MuseScore 能直接吃的音频导出格式
AUDIO_EXT = (".wav", ".mp3", ".flac", ".ogg")

GM_PIANO = {
    "piano": 0, "bright": 1, "electric-grand": 2, "honky-tonk": 3,
    "rhodes": 4, "epiano": 5, "harpsichord": 6, "clav": 7,
}
GM_DRUM_CHANNEL = 9


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
def find_musescore() -> Path | None:
    for c in MUSESCORE_CANDIDATES:
        p = Path(c)
        if p.exists():
            return p
    return None


def hhmmss(x: float) -> str:
    m, s = divmod(max(0.0, x), 60)
    return f"{int(m)}:{s:04.1f}"


class Log:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str = ""):
        print(msg, flush=True)
        self.lines.append(msg)

    def head(self, title: str):
        self("")
        self("=" * 72)
        self(f"  {title}")
        self("=" * 72)


# --------------------------------------------------------------------------- #
# 1. 解剖
# --------------------------------------------------------------------------- #
def note_onsets(inst) -> list[tuple[float, int]]:
    return [(round(n.start, 4), n.pitch) for n in inst.notes]


def similarity(a, b, tol_sec: float = 0.05):
    """两组 (时刻, 音高) 的重合率（起点量化到 tol_sec 后比较）"""
    if not a or not b:
        return 0.0
    qa = Counter((round(t / tol_sec), p) for t, p in a)
    qb = Counter((round(t / tol_sec), p) for t, p in b)
    inter = sum((qa & qb).values())
    return inter / max(len(a), len(b))


def analyze(pm: pretty_midi.PrettyMIDI, lo: float) -> dict:
    """解剖 + 重复轨检测：返回诊断结果"""
    melodic, drums = [], []
    for i, ins in enumerate(pm.instruments):
        (drums if ins.is_drum else melodic).append(i)

    info = {
        "n_instruments": len(pm.instruments),
        "melodic": melodic,
        "drums": drums,
        "duration": pm.get_end_time(),
        "tracks": [],
        "duplicates": [],
    }
    for i, ins in enumerate(pm.instruments):
        pitches = [n.pitch for n in ins.notes]
        info["tracks"].append({
            "index": i,
            "name": ins.name.strip() or f"(轨{i + 1})",
            "program": ins.program,
            "is_drum": bool(ins.is_drum),
            "notes": len(ins.notes),
            "range": [int(min(pitches)), int(max(pitches))] if pitches else None,
            "median_pitch": float(np.median(pitches)) if pitches else None,
        })

    # 打击乐去掉以后再比重复，否则鼓轨天然跟谁都"相似度 0"
    ids = [t["index"] for t in info["tracks"]
           if not t["is_drum"] and t["notes"] >= max(20, lo * 0.02)]
    onsets = {i: note_onsets(pm.instruments[i]) for i in ids}
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            a, b = ids[x], ids[y]
            sim = similarity(onsets[a], onsets[b])
            if sim >= 0.85:
                # 音符多的那条留作主轨（信息更全）
                keep, drop = (a, b) if len(onsets[a]) >= len(onsets[b]) else (b, a)
                info["duplicates"].append({
                    "a": a, "b": b, "sim": sim, "keep": keep, "drop": drop,
                    "notes_a": len(onsets[a]), "notes_b": len(onsets[b]),
                })
    return info


def _jclean(o):
    """把 numpy 标量转成原生 Python 类型，否则 json.dumps 会炸"""
    if isinstance(o, dict):
        return {k: _jclean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jclean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def print_analysis(info: dict, log: Log):
    log("【1/6】解剖原文件")
    log(f"  乐器轨 {info['n_instruments']} 条（演奏 {len(info['melodic'])} + 打击乐 {len(info['drums'])}）"
        f"   时长 {hhmmss(info['duration'])}")
    log("")
    log(f"  {'轨':>3} {'音色':>5} {'音符':>7} {'音域':>10} {'中位音高':>8}  名称")
    log("  " + "-" * 62)
    for t in info["tracks"]:
        rng = f"{t['range'][0]}-{t['range'][1]}" if t["range"] else "—"
        med = f"{t['median_pitch']:.0f}" if t["median_pitch"] is not None else "—"
        tag = "  [打击乐]" if t["is_drum"] else ""
        log(f"  {t['index']:>3} {t['program']:>5} {t['notes']:>7} {rng:>10} {med:>8}  "
            f"{t['name']}{tag}")
    if info["drums"]:
        log(f"\n  ⚠️ 有 {len(info['drums'])} 条打击乐轨 —— 单独静音，不会变成钢琴音")
    if info["duplicates"]:
        log(f"\n  ⚠️ 发现 {len(info['duplicates'])} 组重复声部（同一段音乐叠了两遍）：")
        for d in info["duplicates"]:
            log(f"     轨{d['a']} vs 轨{d['b']}：相似度 {d['sim']:.3f} "
                f"({d['notes_a']} vs {d['notes_b']} 音) → 保留轨{d['keep']}，去掉轨{d['drop']}")
        log("     （不处理的话会响 4 LU 左右并且发糊）")
    else:
        log("\n  ✓ 没有检测到重复声部")


# --------------------------------------------------------------------------- #
# 2. 改音色 + 去重 + 静音打击乐
# --------------------------------------------------------------------------- #
def _safe_midi_text(s: str, limit: int = 60) -> str:
    """把轨名清洗成一定能写进 MIDI 的字符串。

    MIDI 的文本事件（track_name 等）规定是 **latin-1** 编码，mido 写不出去
    非 latin-1 字符时会抛 UnicodeEncodeError，整个文件就废了。

    坑在于有两类"坏轨名"，只处理第一类是不够的：

      1) 真正的非 latin-1 字符，如「音轨 3」—— encode('latin-1') 会抛错。
      2) **乱码**，如「Òô¹ì 3」。这是中文站点的 MIDI 里 GBK 字节被当成
         latin-1 读出来的结果。麻烦点在于这些字符**全都合法落在 latin-1
         范围内**（U+00D2、U+00F4、U+00B9、U+00EC），所以编码不会报错、
         就这么一路传到输出里 —— 实测本项目真实那份 MIDI 就是这样。

    所以这里既剔除无法编码的字符，也剔除典型乱码区（U+0080–U+00FF 里的
    Latin-1 补充符号）。真正的重音字母（像 "Café"）会被牺牲掉，但轨名
    本来就不该承载这些，写出去的谱子能用比保留一个花哨轨名重要得多。
    """
    s = (s or "").strip()
    out = []
    for ch in s:
        o = ord(ch)
        if o < 0x20:                       # 控制字符
            continue
        if 0x7F <= o <= 0xFF:              # 乱码高发区（Latin-1 补充）
            continue
        if o > 0xFF:                       # 非 latin-1，根本写不出去
            continue
        out.append(ch)                     # 剩下来的都是可打印 ASCII
    return ("".join(out).strip() or "")[:limit]


def make_piano(pm: pretty_midi.PrettyMIDI, info: dict, drop: set[int],
               program: int, mute_drums: bool, beat_sec: float) -> dict:
    """就地改：音色统一、删重复轨、静音鼓；返回统计"""
    stat = Counter()
    for i, ins in enumerate(pm.instruments):
        ins.name = _safe_midi_text(ins.name) or f"Track {i + 1}"
        if i in drop:
            stat["删除重复轨"] += 1
            ins.notes = []
            ins.control_changes = []
            ins.pitch_bends = []
            ins.name = _safe_midi_text(f"(muted) {ins.name}")
            continue
        if ins.is_drum:
            if mute_drums:
                stat["静音打击乐"] += 1
                ins.notes = []          # 整轨清掉，比把 velocity 改 0 更干净
                ins.name = _safe_midi_text(f"(muted) {ins.name}")
            continue
        if ins.program != program:
            stat["改音色"] += 1
        ins.program = program
        ins.is_drum = False
    # 清空还不够：pretty_midi 会为每个 instrument 写一条轨并带上 program_change，
    # 被删/被静音的轨会留下一条"空轨 + 原音色号"。直接从列表里摘掉才干净。
    pm.instruments = [ins for ins in pm.instruments if ins.notes]
    return dict(stat)


# --------------------------------------------------------------------------- #
# 3. 修重击（同音高零间隔）
# --------------------------------------------------------------------------- #
def fix_retrigger(pm: pretty_midi.PrettyMIDI, gap_sec: float) -> tuple[int, int]:
    """同一 (轨, 音高) 上相邻两音间隔 < gap 的，把前一个音的尾部削短。

    只动 note.end，不动 note.start —— 节奏一个字节都不变。
    """
    adjusted = 0
    scanned = 0
    for ins in pm.instruments:
        if not ins.notes:
            continue
        by_pitch: dict[int, list] = {}
        for n in ins.notes:
            by_pitch.setdefault(n.pitch, []).append(n)
        for _pitch, lst in by_pitch.items():
            lst.sort(key=lambda n: (n.start, n.end))
            for k in range(len(lst) - 1):
                scanned += 1
                cur, nxt = lst[k], lst[k + 1]
                if nxt.start - cur.end < gap_sec:
                    target = max(cur.start + 1e-4, nxt.start - gap_sec)
                    if target < cur.end - 1e-6:
                        cur.end = target
                        adjusted += 1
    return adjusted, scanned


# --------------------------------------------------------------------------- #
# 4. 左右手分配
# --------------------------------------------------------------------------- #
def assign_hands(notes: list[pretty_midi.Note], split_pitch: int) -> tuple[list, list]:
    """按音高阈值分左右手。单一轨 MIDI 只能这么分（轨序号跟上下声部无关）。"""
    rh = [n for n in notes if n.pitch >= split_pitch]
    lh = [n for n in notes if n.pitch < split_pitch]
    return rh, lh


def choose_split(notes: list[pretty_midi.Note], lo: int, hi: int) -> tuple[int, float]:
    """在 lo..hi 里挑一个让左右手"音符数最接近"的分界音高"""
    pitches = np.array([n.pitch for n in notes])
    total = len(pitches)
    best, best_cost = 60, 1e18
    for s in range(lo, hi + 1):
        n_rh = int((pitches >= s).sum())
        n_lh = total - n_rh
        if n_rh == 0 or n_lh == 0:
            continue
        cost = abs(n_rh - n_lh)
        if cost < best_cost:
            best, best_cost = s, cost
    ratio = float((pitches >= best).sum()) / max(1, total)
    return best, ratio


# --------------------------------------------------------------------------- #
# 5. 写钢琴谱 + 结构校验 + 重分小节
# --------------------------------------------------------------------------- #

# ---- MusicXML 结构校验 ------------------------------------------------------ #
def validate_musicxml(path: Path, ts_num: int, ts_den: int) -> dict:
    """按 MusicXML 的播放语义（note/rest 前进、forward 前进、backup 后退）
    模拟每个 part 的游标，检查小节是否被撑爆。
    """
    res = {"ok": True, "problems": [], "parts": [], "n_measures": 0}
    try:
        tree = ET.parse(str(path))
    except Exception as e:
        res["ok"] = False
        res["problems"].append(f"XML 解析失败: {type(e).__name__}: {e}")
        return res

    root = tree.getroot()
    if not root.tag.endswith("score-partwise"):
        res["ok"] = False
        res["problems"].append(f"根元素不是 score-partwise（是 {root.tag}）")
        return res

    n_parts = len(root.findall("./part"))
    if n_parts < 1:
        res["ok"] = False
        res["problems"].append("没有任何 part（空谱）")
        return res

    measures_per_part = []
    for pi, part in enumerate(root.findall("./part")):
        measures = part.findall("./measure")
        measures_per_part.append(len(measures))
        divisions = None
        overflow = []
        for mi, m in enumerate(measures, 1):
            cursor = 0.0
            voice_sum: dict[str, float] = {}
            for child in m:
                tag = child.tag
                if tag == "attributes":
                    d = child.findtext("divisions")
                    if d:
                        divisions = float(d)
                elif tag == "note":
                    if child.find("grace") is not None:
                        continue
                    v = child.findtext("voice") or "1"
                    if child.find("chord") is not None:
                        continue          # 和弦音与前一音同时发声，不累加时值
                    dur = child.findtext("duration")
                    dur = float(dur) if dur else 0.0
                    cursor += dur
                    voice_sum[v] = voice_sum.get(v, 0.0) + dur
                elif tag == "forward":
                    dur = child.findtext("duration")
                    cursor += float(dur) if dur else 0.0
                elif tag == "backup":
                    dur = child.findtext("duration")
                    cursor -= float(dur) if dur else 0.0
            if divisions:
                # divisions = 每个**四分音符**的分度 → cursor/divisions = 几个四分音符
                bar_ql = ts_num * 4.0 / ts_den
                ql_per_beat = 4.0 / ts_den
                got_ql = cursor / divisions
                worst_v, worst_ql = None, got_ql
                for v, s in voice_sum.items():
                    if s / divisions > worst_ql:
                        worst_v, worst_ql = v, s / divisions
                if worst_ql > bar_ql * 1.02 + 0.01:
                    who = f"声部{worst_v}" if worst_v is not None else "全谱"
                    overflow.append((mi, round(worst_ql / ql_per_beat, 2),
                                     float(ts_num), who))
        res["parts"].append({"index": pi, "measures": len(measures),
                             "overflow": overflow[:5], "n_overflow": len(overflow)})
        if overflow:
            res["ok"] = False
            res["problems"].append(
                f"part{pi}: {len(overflow)} 个小节被撑爆，"
                f"例如第 {overflow[0][0]} 小节 {overflow[0][3]} 装了 {overflow[0][1]} 拍"
                f"（应为 {overflow[0][2]} 拍）")
        if not divisions:
            res["ok"] = False
            res["problems"].append(f"part{pi}: 没有 <divisions>，无法校验时值")

    res["n_measures"] = max(measures_per_part) if measures_per_part else 0
    if len(set(measures_per_part)) > 1:
        res["ok"] = False
        res["problems"].append(
            f"各声部小节数不一致：{measures_per_part}（MuseScore 会拒收）")
    return res


# ---- 重分小节修复 ----------------------------------------------------------- #
def rebar_notes(notes, ts_num, ts_den, bpm, grid=0.25):
    """把一个声部的音符按绝对时间摊平、用给定拍号重新分小节。

    对"小节被撑爆"的谱子，这是唯一干净的修法：不去猜坏结构，
    直接把 (onset, dur) 按网格对齐后落到正确的小节位置上。
    """
    bar_ql = ts_num * 4.0 / ts_den
    qpm = bpm / 60.0
    out = []
    for n in notes:
        onset_ql = n.start * qpm
        dur_ql = max((n.end - n.start) * qpm, grid)
        onset_q = max(0.0, round(onset_ql / grid) * grid)
        dur_q = max(grid, round(dur_ql / grid) * grid)
        out.append((onset_q, dur_q, n.pitch, n.velocity))
    return out


def write_score_xml(events_by_part, ts_num, ts_den, bpm, out_path: Path,
                    title=None, key_name=None):
    """不经过 music21，直接写一份干净的 MusicXML（小节由我们控制，绝不撑爆）"""
    bar_ql = ts_num * 4.0 / ts_den
    DIVS = 960                      # 每四分音符的分度
    grid = 0.25                     # 量化网格（四分音符）

    def qdiv(ql: float) -> int:
        return max(1, int(round(ql * DIVS)))

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 3.1 Partwise//EN" '
             '"http://www.musicxml.org/dtds/partwise.dtd">',
             '<score-partwise version="3.1">']
    if title:
        lines.append(f"  <work><work-title>{_esc(title)}</work-title></work>")
        lines.append(f"  <movement-title>{_esc(title)}</movement-title>")
    lines.append("  <part-list>")
    for pid in ("P1", "P2"):
        lines.append(f'    <score-part id="{pid}"><part-name>Piano</part-name>'
                     f'<score-instrument id="{pid}-I1"><instrument-name>Piano</instrument-name>'
                     f'</score-instrument>'
                     f'<midi-instrument id="{pid}-I1"><midi-channel>1</midi-channel>'
                     f'<midi-program>1</midi-program></midi-instrument></score-part>')
    lines.append("  </part-list>")

    # 每个 part 的事件按绝对四分音符位置分组，再切成小节
    measures_per_part = []
    # 先把每个 part 的事件按小节分桶，并算出全局统一的小节数。
    # MusicXML 要求一份谱里所有 part 的小节数完全相同，否则 MuseScore 直接拒收。
    per_part_buckets: list[list[list]] = []
    for evs in events_by_part:
        by_onset: dict[float, list] = {}
        for onset_q, dur_q, pitch, vel in evs:
            by_onset.setdefault(round(onset_q / grid) * grid, []).append((dur_q, pitch, vel))
        onsets = sorted(by_onset)
        total_ql = max((o for o in onsets), default=0.0) + max(
            (max(d for d, _, _ in by_onset[o]) for o in onsets), default=0.0)
        n_meas = max(1, int(math.ceil(total_ql / bar_ql - 1e-9)))
        buckets: list[list] = [[] for _ in range(n_meas)]
        for o in onsets:
            mi = int(o // bar_ql)
            if mi >= n_meas:
                mi = n_meas - 1
            for dur_q, pitch, vel in by_onset[o]:
                buckets[mi].append((o - mi * bar_ql, dur_q, pitch, vel))
        per_part_buckets.append(buckets)

    n_meas_total = max((len(b) for b in per_part_buckets), default=1)
    for b in per_part_buckets:                   # 补齐到统一小节数
        while len(b) < n_meas_total:
            b.append([])
    measures_per_part = [len(b) for b in per_part_buckets]

    for pi, buckets in enumerate(per_part_buckets):
        pid = f"P{pi + 1}"
        lines.append(f'  <part id="{pid}">')
        for mi, bucket in enumerate(buckets):
            lines.append(f'    <measure number="{mi + 1}">')
            if mi == 0:
                lines.append(f"      <attributes>"
                             f"<divisions>{DIVS}</divisions>"
                             f"<key><fifths>{_fifths(key_name)}</fifths></key>"
                             f"<time><beats>{ts_num}</beats><beat-type>{ts_den}</beat-type></time>"
                             f"<clef><sign>{'G' if pi == 0 else 'F'}</sign>"
                             f"<line>{'2' if pi == 0 else '4'}</line></clef>"
                             f"</attributes>")
                lines.append(f'      <direction placement="above"><direction-type>'
                             f'<metronome><beat-unit>quarter</beat-unit>'
                             f'<per-minute>{round(bpm)}</per-minute></metronome>'
                             f'</direction-type><sound tempo="{round(bpm)}"/></direction>')
            # 小节内串行写出。三条硬规则，保证本小节时值恒等于小节长度：
            #   a) 每个音的时值 = min(自己的长度, 到下一个音起点的距离)
            #      —— 不把长音尾挤到下一个音上（这就是"小节撑爆"的成因）
            #   b) 只写"落在本小节内、且在游标之后"的音
            #   c) 音符之间的空隙补休止，末尾补休止到小节精确结束
            bucket.sort(key=lambda e: (e[0], -e[2]))
            budget = int(round(bar_ql / grid))       # 本小节总网格数
            used = 0                                 # 已用网格数
            placed = []
            for off, dur_q, pitch, vel in bucket:
                start_g = int(round(off / grid))
                if start_g < used:                   # 与已写内容重叠 → 丢弃（消音）
                    continue
                if start_g >= budget:                # 落到小节外 → 丢弃
                    continue
                room_g = budget - start_g
                # 到下一个不同起点的距离（同起点的和弦音属同一批，不互相截断）
                nxt = [int(round(o / grid)) for o, _, _, _ in bucket if o > off + 1e-9]
                dur_g = max(1, int(round(dur_q / grid)))
                if nxt:
                    dur_g = min(dur_g, min(nxt) - start_g)
                dur_g = max(1, min(dur_g, room_g))
                placed.append((start_g, dur_g, pitch, vel))
                used = max(used, start_g + dur_g)

            written = 0
            for start_g, dur_g, pitch, vel in placed:
                if start_g > written:                # 空隙 → 休止
                    gap = (start_g - written) * grid
                    lines.append(f"      <note><rest/><duration>{qdiv(gap)}</duration>"
                                 f"<voice>1</voice><type>{_type_of(gap)}</type></note>")
                    written = start_g
                d = dur_g * grid
                lines.append(
                    f"      <note><pitch><step>{_STEP[pitch % 12]}</step>"
                    f"<alter>{_ALTER[pitch % 12]}</alter>"
                    f"<octave>{pitch // 12 - 1}</octave></pitch>"
                    f"<duration>{qdiv(d)}</duration><voice>1</voice>"
                    f"<type>{_type_of(d)}</type>"
                    f"<velocity>{int(vel)}</velocity></note>")
                written = start_g + dur_g
            if written < budget:                     # 末尾休止，正好补满
                gap = (budget - written) * grid
                lines.append(f"      <note><rest/><duration>{qdiv(gap)}</duration>"
                             f"<voice>1</voice><type>{_type_of(gap)}</type></note>")
                written = budget
            if written != budget:
                raise AssertionError(
                    f"小节 {mi + 1} 时值不自洽：写了 {written} 格，应为 {budget} 格")
            lines.append("    </measure>")
        lines.append("  </part>")
    lines.append("</score-partwise>")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return max(measures_per_part) if measures_per_part else 0


_STEP = ["C", "C", "D", "D", "E", "F", "F", "G", "G", "A", "A", "B"]
_ALTER = [0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 1, 0]


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _fifths(key_name: str | None) -> int:
    if not key_name:
        return 0
    m = {"C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5, "F#": 6, "C#": 7,
         "F": -1, "Bb": -2, "Eb": -3, "Ab": -4, "Db": -5, "Gb": -6, "Cb": -7}
    k = key_name.strip()
    if k.endswith("m"):
        k = k[:-1]
    k = k[:1].upper() + k[1:]
    return m.get(k, 0)


def _type_of(ql: float) -> str:
    table = [(4.0, "whole"), (3.0, "half"), (2.0, "half"), (1.5, "quarter"),
             (1.0, "quarter"), (0.75, "eighth"), (0.5, "eighth"),
             (0.25, "16th")]
    for v, name in table:
        if ql >= v - 1e-6:
            return name
    return "16th"


# --------------------------------------------------------------------------- #
# 6. 渲染音频 + 响度
# --------------------------------------------------------------------------- #
def render_audio(musescore: Path, src: Path, wav: Path, log: Log,
                 xml: Path | None = None) -> bool:
    for cand in ([src] + ([xml] if xml else [])):
        if cand is None or not cand.exists():
            continue
        try:
            # 注意：不能用 text=True —— MuseScore 在 Windows 上会往 stdout 吐
            # 非 GBK 字节，text 模式解码会抛 UnicodeDecodeError 把渲染线程搞崩
            r = subprocess.run([str(musescore), "-o", str(wav), str(cand)],
                               capture_output=True, timeout=1800)
        except subprocess.TimeoutExpired:
            log(f"    ✗ 渲染超时（{cand.name}）")
            continue
        if wav.exists() and wav.stat().st_size > 1024:
            if r.returncode != 0:
                log(f"    （{cand.name} 返回码 {r.returncode}，但音频已生成，继续）")
            return True
        log(f"    ✗ {cand.name} 渲染失败（返回码 {r.returncode}）")
        tail = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        for line in tail[-3:]:
            log(f"        {line.strip()[:110]}")
    return False


def normalize_loudness(x, sr, target_lufs, ceiling=0.92):
    info = {}
    try:
        import pyloudnorm as pyln
        before = pyln.Meter(sr).integrated_loudness(x)
        if np.isfinite(before):
            info["before_lufs"] = round(float(before), 2)
            x = x * (10.0 ** ((target_lufs - before) / 20.0))
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    info["peak_pre"] = round(float(np.max(np.abs(x))), 3)
    if info["peak_pre"] > ceiling:
        from scipy.ndimage import maximum_filter1d, uniform_filter1d
        peak = np.abs(x).max(axis=1) if x.ndim > 1 else np.abs(x)
        la = int(sr * 0.005)
        rel = int(sr * 0.12)
        env = maximum_filter1d(peak, size=max(1, la), mode="nearest")
        gain = np.minimum(1.0, ceiling / np.maximum(env, 1e-9))
        gain = uniform_filter1d(gain, size=max(1, rel), mode="nearest")
        x = x * (gain[:, None] if x.ndim > 1 else gain)
        np.clip(x, -ceiling, ceiling, out=x)
        info["limiter_gr"] = round(float(20 * np.log10(max(gain.min(), 1e-9))), 2)
    try:
        import pyloudnorm as pyln
        info["after_lufs"] = round(float(pyln.Meter(sr).integrated_loudness(x)), 2)
    except Exception:
        pass
    info["peak"] = round(float(np.max(np.abs(x))), 3)
    return x.astype(np.float32), info


# --------------------------------------------------------------------------- #
