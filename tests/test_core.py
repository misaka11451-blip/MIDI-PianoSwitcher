"""pianoize 的核心测试。

这些测试**不依赖 MuseScore**：音频渲染是可选环节，这里只测纯逻辑，
所以 CI 上不用装任何外部程序也能跑。

跑法：

    pytest -q
"""
from __future__ import annotations

import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import mido
import pretty_midi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pianoize import core  # noqa: E402

TPB = 480


# --------------------------------------------------------------------------- #
# 工具：造 MIDI
# --------------------------------------------------------------------------- #
def make_midi(path: Path, tracks, tpb: int = TPB, tempo_bpm: float = 120.0,
              time_sig=(4, 4)) -> Path:
    """tracks: [(name, program, channel, [(start_tick, dur_tick, pitch, vel), ...]), ...]"""
    m = mido.MidiFile(type=1, ticks_per_beat=tpb)
    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    meta.append(mido.MetaMessage("time_signature", numerator=time_sig[0],
                                 denominator=time_sig[1], time=0))
    meta.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(tempo_bpm), time=0))
    m.tracks.append(meta)
    for name, program, channel, notes in tracks:
        tr = mido.MidiTrack()
        tr.append(mido.MetaMessage("track_name", name=name, time=0))
        tr.append(mido.Message("program_change", channel=channel,
                               program=program, time=0))
        events = []
        for start, dur, pitch, vel in notes:
            events.append((start, mido.Message("note_on", channel=channel,
                                               note=pitch, velocity=vel)))
            events.append((start + dur, mido.Message("note_off", channel=channel,
                                                     note=pitch, velocity=0)))
        prev = 0
        for tick, msg in sorted(events, key=lambda e: e[0]):
            msg.time = max(0, tick - prev)
            prev = tick
            tr.append(msg)
        m.tracks.append(tr)
    m.save(str(path))
    return path


def scale_notes(n=16, start=60, dur=TPB // 2, step=TPB // 2):
    return [(i * step, dur, start + (i % 5), 80) for i in range(n)]


# --------------------------------------------------------------------------- #
# 1. 重复轨检测
# --------------------------------------------------------------------------- #
def test_detect_duplicate_tracks(tmp_path):
    """音乐完全相同的两条轨，应被判定为重复，并保留音符多的那条。"""
    tr_a = ("Lead", 0, 0, scale_notes(20))
    tr_b = ("Copy", 11, 1, scale_notes(20))
    p = make_midi(tmp_path / "dup.mid", [tr_a, tr_b])
    pm = pretty_midi.PrettyMIDI(str(p))
    info = core.analyze(pm, lo=120.0)
    assert len(info["duplicates"]) == 1, info["duplicates"]
    d = info["duplicates"][0]
    assert d["sim"] >= 0.85
    assert d["drop"] in (0, 1) and d["keep"] in (0, 1)
    assert d["keep"] != d["drop"]


def test_no_false_duplicate_on_different_music(tmp_path):
    """两条不同的旋律不能被误判成重复。"""
    tr_a = ("High", 0, 0, scale_notes(20, start=72))
    tr_b = ("Low", 32, 1, [(i * TPB, TPB - 10, 45 + (i % 3), 70) for i in range(20)])
    p = make_midi(tmp_path / "diff.mid", [tr_a, tr_b])
    pm = pretty_midi.PrettyMIDI(str(p))
    info = core.analyze(pm, lo=120.0)
    assert info["duplicates"] == []


def test_drum_track_not_treated_as_duplicate(tmp_path):
    """鼓轨不该跟任何东西被判定重复（时间网格碰巧重合也不行）。"""
    drum = ("Drums", 0, 9, [(i * TPB, 60, 36, 100) for i in range(20)])
    p = make_midi(tmp_path / "drum.mid", [drum])
    pm = pretty_midi.PrettyMIDI(str(p))
    info = core.analyze(pm, lo=120.0)
    assert info["drums"] == [0]
    assert info["duplicates"] == []


# --------------------------------------------------------------------------- #
# 2. 改音色 / 静音鼓 / 清理空轨
# --------------------------------------------------------------------------- #
def test_program_change_to_piano_and_drum_mute(tmp_path):
    tracks = [
        ("Melody", 11, 0, scale_notes(8)),
        ("Bass", 32, 1, [(i * TPB, TPB - 10, 40, 70) for i in range(8)]),
        ("Drums", 0, 9, [(i * TPB, 60, 36, 100) for i in range(8)]),
    ]
    p = make_midi(tmp_path / "mix.mid", tracks)
    pm = pretty_midi.PrettyMIDI(str(p))
    info = core.analyze(pm, lo=120.0)
    stat = core.make_piano(pm, info, drop=set(), program=0, mute_drums=True,
                           beat_sec=0.5)
    assert stat.get("改音色", 0) >= 1
    assert stat.get("静音打击乐") == 1

    out = tmp_path / "out.mid"
    pm.write(str(out))
    mm = mido.MidiFile(str(out))
    progs = {msg.program for tr in mm.tracks for msg in tr
             if msg.type == "program_change"}
    assert progs == {0}, f"音色没统一成钢琴：{progs}"
    drum_notes = [msg for tr in mm.tracks for msg in tr
                  if msg.type == "note_on" and msg.velocity > 0 and msg.channel == 9]
    assert drum_notes == [], "鼓没被静音"
    # 被静音的轨不该留下空轨
    assert len(pm.instruments) == 2


def test_empty_tracks_are_dropped(tmp_path):
    tracks = [("A", 0, 0, scale_notes(8)), ("B", 24, 1, scale_notes(8, start=50))]
    p = make_midi(tmp_path / "two.mid", tracks)
    pm = pretty_midi.PrettyMIDI(str(p))
    info = core.analyze(pm, lo=120.0)
    core.make_piano(pm, info, drop={1}, program=0, mute_drums=True, beat_sec=0.5)
    assert len(pm.instruments) == 1


# --------------------------------------------------------------------------- #
# 3. 重击修复：只动结束时刻，不起音
# --------------------------------------------------------------------------- #
def test_retrigger_fix_moves_only_note_end(tmp_path):
    """同一音高零间隔重击 → 前一个音要被削短；起音时刻必须一个不差。"""
    notes = []
    for i in range(6):
        t = i * TPB
        notes.append((t, TPB, 64, 80))          # 首尾相接 → 零间隔
    p = make_midi(tmp_path / "rt.mid", [("Mono", 0, 0, notes)])

    pm = pretty_midi.PrettyMIDI(str(p))
    before_starts = sorted(round(n.start, 6) for n in pm.instruments[0].notes)
    adjusted, scanned = core.fix_retrigger(pm, gap_sec=0.02)
    assert scanned > 0
    assert adjusted >= 4, f"应削短大部分重击，实际 {adjusted}"
    after_starts = sorted(round(n.start, 6) for n in pm.instruments[0].notes)
    assert before_starts == after_starts, "起音时刻被改动了！"
    # 相邻两音之间要留出间隙
    lst = sorted(pm.instruments[0].notes, key=lambda n: n.start)
    for a, b in zip(lst, lst[1:]):
        assert b.start - a.end >= 0.02 - 1e-6, f"间隙不足：{b.start - a.end}"


def test_retrigger_fix_leaves_clean_notes_alone(tmp_path):
    """间隔本来就够大的音符不该被动。"""
    notes = [(i * TPB * 2, TPB, 60 + i, 80) for i in range(5)]
    p = make_midi(tmp_path / "clean.mid", [("Solo", 0, 0, notes)])
    pm = pretty_midi.PrettyMIDI(str(p))
    ends_before = sorted(round(n.end, 6) for n in pm.instruments[0].notes)
    adjusted, _ = core.fix_retrigger(pm, gap_sec=0.02)
    assert adjusted == 0
    assert ends_before == sorted(round(n.end, 6) for n in pm.instruments[0].notes)


# --------------------------------------------------------------------------- #
# 4. 左右手分配
# --------------------------------------------------------------------------- #
def test_assign_hands_by_pitch():
    notes = [pretty_midi.Note(80, 60, 0, 1), pretty_midi.Note(80, 90, 0, 1),
             pretty_midi.Note(80, 50, 0, 1)]
    rh, lh = core.assign_hands(notes, split_pitch=60)
    assert [n.pitch for n in rh] == [60, 90]
    assert [n.pitch for n in lh] == [50]


def test_choose_split_balances_when_all_notes_high():
    """所有音都高于阈值时，choose_split 要能找出一个让两手都不为空的界。"""
    notes = [pretty_midi.Note(80, p, 0, 1) for p in (72, 74, 76, 78, 80, 82)]
    split, ratio = core.choose_split(notes, 72, 82)
    rh, lh = core.assign_hands(notes, split)
    assert rh and lh, f"split={split} 仍然把音符全挤到一边"
    assert 0.0 < ratio < 1.0


# --------------------------------------------------------------------------- #
# 5. 写谱 + 结构校验（本项目的核心护栏）
# --------------------------------------------------------------------------- #
def _events(notes, bpm=120.0, grid=0.25):
    out = []
    for n in notes:
        onset_ql = n.start * bpm / 60.0
        dur_ql = max((n.end - n.start) * bpm / 60.0, grid)
        out.append((max(0.0, round(onset_ql / grid) * grid),
                    max(grid, round(dur_ql / grid) * grid), n.pitch, n.velocity))
    return out


def test_written_score_passes_validation(tmp_path):
    notes_a = [pretty_midi.Note(80, 72 + (i % 5), i * 0.5, i * 0.5 + 0.45)
               for i in range(30)]
    notes_b = [pretty_midi.Note(75, 48, i * 1.0, i * 1.0 + 0.9) for i in range(15)]
    out = tmp_path / "s.musicxml"
    n = core.write_score_xml([_events(notes_a), _events(notes_b)], 4, 4, 120.0, out)
    assert n >= 1 and out.exists()
    v = core.validate_musicxml(out, 4, 4)
    assert v["ok"], v["problems"]
    assert v["n_measures"] == n
    assert len(v["parts"]) == 2
    assert all(p["n_overflow"] == 0 for p in v["parts"])


def test_long_note_does_not_overflow_measure(tmp_path):
    """超长音（跨多个小节）不得把后续小节撑爆 —— 这是最经典的成因。"""
    notes = [pretty_midi.Note(70, 60, 0.0, 8.0)]                    # 撑 4 个小节
    notes += [pretty_midi.Note(80, 67, i * 0.25, i * 0.25 + 0.2)
              for i in range(40)]
    out = tmp_path / "long.musicxml"
    core.write_score_xml([_events(notes), _events([])], 4, 4, 120.0, out)
    v = core.validate_musicxml(out, 4, 4)
    assert v["ok"], v["problems"]


def test_both_parts_get_same_measure_count(tmp_path):
    """两个 part 的音符结束时刻不同时，小节数也必须取齐，否则 MuseScore 拒收。"""
    notes_a = [pretty_midi.Note(80, 72, i * 0.5, i * 0.5 + 0.4) for i in range(40)]
    notes_b = [pretty_midi.Note(80, 48, i * 1.0, i * 1.0 + 0.4) for i in range(6)]
    out = tmp_path / "uneven.musicxml"
    core.write_score_xml([_events(notes_a), _events(notes_b)], 4, 4, 120.0, out)
    counts = [len(p.findall("./measure")) for p in ET.parse(str(out)).getroot()
              .findall("./part")]
    assert counts[0] == counts[1], f"小节数不一致：{counts}"
    assert core.validate_musicxml(out, 4, 4)["ok"]


def test_non_four_four_time_signature(tmp_path):
    """3/4 拍要能正确分小节并通过校验（每小节 3 个四分音符）。

    音符间隔取 0.375 秒 = 120BPM 下的 0.75 个四分音符（附点八分），
    正好落在默认 0.25 网格上。36 个音跨 27.75 ql → 3/4 拍下 9 个小节。
    """
    notes = [pretty_midi.Note(80, 60 + (i % 7), i * 0.375, i * 0.375 + 0.3)
             for i in range(36)]
    out = tmp_path / "waltz.musicxml"
    n = core.write_score_xml([_events(notes), _events([])], 3, 4, 120.0, out)
    v = core.validate_musicxml(out, 3, 4)
    assert v["ok"], v["problems"]
    assert n == 9, f"应为 9 小节（27.75 ql / 3），实际 {n}"


def test_triplet_grid_is_finer_than_default(tmp_path):
    """三连音素材：默认 0.25 网格会量化漂移，换更细的网格（1/12）才对齐。

    这是真实取舍（不是 bug）：--grid 越细，越能保住人性化节奏与三连音，
    但谱面看起来越碎。这里把它固化成测试，免得以后误以为网格无关紧要。
    """
    # 八分三连音：每 0.5 秒 = 1.0 ql，即 0.3333 ql 间隔
    notes = [pretty_midi.Note(80, 60 + (i % 7), i / 6.0, i / 6.0 + 0.12)
             for i in range(36)]

    def measure_count(grid):
        out = tmp_path / f"tri_{str(grid).replace('/', '_')}.musicxml"
        n = core.write_score_xml([_events(notes, grid=grid), _events([])],
                                 4, 4, 120.0, out)
        return n, core.validate_musicxml(out, 4, 4)

    n_coarse, v_coarse = measure_count(0.25)
    n_fine, v_fine = measure_count(1.0 / 12)
    assert v_coarse["ok"] and v_fine["ok"]
    # 素材总长约 6 ql → 2 个小节；细网格应更接近真实长度
    assert n_fine <= n_coarse, (n_fine, n_coarse)


def test_measure_content_exactly_fills_bar(tmp_path):
    """每个小节的时值必须精确等于小节长度（不多不少）。"""
    notes = [pretty_midi.Note(80, 60, i * 0.3, i * 0.3 + 0.25) for i in range(50)]
    out = tmp_path / "exact.musicxml"
    core.write_score_xml([_events(notes), _events([])], 4, 4, 120.0, out)
    root = ET.parse(str(out)).getroot()
    for part in root.findall("./part"):
        divs = None
        for measure in part.findall("./measure"):
            cur = 0.0
            for child in measure:
                if child.tag == "attributes" and child.findtext("divisions"):
                    divs = float(child.findtext("divisions"))
                elif child.tag == "note" and child.find("chord") is None \
                        and child.find("grace") is None:
                    cur += float(child.findtext("duration") or 0)
            assert divs
            got_ql = cur / divs
            assert abs(got_ql - 4.0) < 1e-6, f"小节时值 {got_ql}，应为 4.0"


# --------------------------------------------------------------------------- #
# 6. 校验器必须能抓出「坏谱」—— 否则护栏没意义
# --------------------------------------------------------------------------- #
def test_validator_catches_voice_overflow(tmp_path):
    """构造一份"某声部超出小节"的谱，校验器必须报错。

    这正是 MuseScore 返回 1320 拒收的真实成因（实测某 OMR 输出第 1 小节
     voice 0 装了 4.25 拍）。只看小节总时值会漏报，必须逐声部累加。
    """
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>4</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>
        <voice>0</voice></note>
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>8</duration>
        <voice>0</voice></note>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>8</duration>
        <voice>0</voice></note>
      <backup><duration>17</duration></backup>
      <note><pitch><step>C</step><octave>3</octave></pitch><duration>16</duration>
        <voice>1</voice></note>
    </measure>
  </part>
</score-partwise>"""
    p = tmp_path / "bad.musicxml"
    p.write_text(xml, encoding="utf-8")
    v = core.validate_musicxml(p, 4, 4)
    assert not v["ok"], "校验器没抓出被撑爆的声部——护栏失效"
    assert v["parts"][0]["n_overflow"] == 1
    kind = v["parts"][0]["overflow"][0]
    assert kind[3] == "声部0", kind
    assert kind[1] == pytest.approx(4.25, abs=0.01), kind


def test_validator_catches_uneven_part_measure_counts(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
  <part-list>
    <score-part id="P1"><part-name>Piano</part-name></score-part>
    <score-part id="P2"><part-name>Piano</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1"><attributes><divisions>4</divisions>
      <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><rest/><duration>16</duration></note></measure>
    <measure number="2"><note><rest/><duration>16</duration></note></measure>
  </part>
  <part id="P2">
    <measure number="1"><attributes><divisions>4</divisions>
      <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><rest/><duration>16</duration></note></measure>
  </part>
</score-partwise>"""
    p = tmp_path / "uneven_bad.musicxml"
    p.write_text(xml, encoding="utf-8")
    v = core.validate_musicxml(p, 4, 4)
    assert not v["ok"]
    assert any("小节数不一致" in s for s in v["problems"]), v["problems"]


def test_validator_rejects_garbage(tmp_path):
    p = tmp_path / "garbage.musicxml"
    p.write_text("this is not xml at all <<<", encoding="utf-8")
    v = core.validate_musicxml(p, 4, 4)
    assert not v["ok"]
    assert v["problems"]


# --------------------------------------------------------------------------- #
# 7. MIDI 文本编码兜底
# --------------------------------------------------------------------------- #
def test_safe_midi_text_strips_non_latin1():
    """MIDI 文本事件是 latin-1；中文轨名必须被兜住，否则整个文件写不出去。"""
    got = core._safe_midi_text("音轨 3")
    assert got.isascii(), got
    assert core._safe_midi_text("Piano") == "Piano"
    assert core._safe_midi_text("") == ""
    assert len(core._safe_midi_text("x" * 200)) == 60


def test_bank_select_is_zeroed(tmp_path):
    """Bank Select（CC0/CC32）必须归零。

    为什么：GM 的音色号只在 Bank 0 里是"钢琴"。源文件一旦带着非零 Bank，
    同一个 program 0 在合成器眼里可能是完全不同的乐器组 —— 这是
    "说好的纯钢琴却听到别的乐器"的一种真实来源。所以除了设 program，
    还要把音色库选择压回 0。
    """
    tracks = [("Melody", 0, 0, scale_notes(30))]
    p = make_midi(tmp_path / "bank.mid", tracks)

    pm = pretty_midi.PrettyMIDI(str(p))
    ins = pm.instruments[0]
    ins.control_changes.append(pretty_midi.ControlChange(0, 8, 0.0))    # Bank MSB
    ins.control_changes.append(pretty_midi.ControlChange(32, 3, 0.0))   # Bank LSB
    ins.control_changes.append(pretty_midi.ControlChange(7, 100, 0.0))  # 音量（不该动）

    info = core.analyze(pm, lo=120.0)
    stat = core.make_piano(pm, info, drop=set(), program=0, mute_drums=True,
                           beat_sec=0.5)
    assert stat.get("归零音色库") == 2, stat

    banks = {c.number: c.value for c in pm.instruments[0].control_changes
             if c.number in (0, 32)}
    assert banks == {0: 0, 32: 0}, banks
    # 其它控制器不受影响
    vol = [c.value for c in pm.instruments[0].control_changes if c.number == 7]
    assert vol == [100], vol


def test_bank_select_zero_is_not_counted(tmp_path):
    """本来就是 0 的 Bank Select 不该算作"归零"过。"""
    p = make_midi(tmp_path / "bank0.mid", [("M", 0, 0, scale_notes(30))])
    pm = pretty_midi.PrettyMIDI(str(p))
    pm.instruments[0].control_changes.append(
        pretty_midi.ControlChange(0, 0, 0.0))
    info = core.analyze(pm, lo=120.0)
    stat = core.make_piano(pm, info, drop=set(), program=0, mute_drums=True,
                           beat_sec=0.5)
    assert stat.get("归零音色库", 0) == 0, stat


def test_chinese_track_name_survives_write(tmp_path):
    """带中文轨名的 MIDI，经 make_piano 之后必须能写出去（不抛 UnicodeEncodeError）。

    注意：不能用 mido 造这个文件 —— mido 自己就写不出中文轨名（latin-1 限制），
    所以这里直接拼原始 MIDI 字节，用 GBK 编码轨名，模拟真实世界那种
    "中文站点下载的 MIDI、轨名是 GBK 字节" 的情况。这也正是本项目真实遇到过的
    那份文件（轨名读出来是 'Òô¹ì 3'）。
    """
    def vlq(n: int) -> bytes:
        out = [n & 0x7F]
        n >>= 7
        while n:
            out.append((n & 0x7F) | 0x80)
            n >>= 7
        return bytes(reversed(out))

    name_bytes = "音轨 3".encode("gbk")          # 中文轨名的原始字节
    ev = bytearray()
    ev += vlq(0) + b"\xff\x03" + vlq(len(name_bytes)) + name_bytes   # track_name
    ev += vlq(0) + b"\xff\x51\x03" + (500000).to_bytes(3, "big")     # set_tempo
    ev += vlq(0) + b"\xc0\x0b"                                       # program_change 11
    ev += vlq(0) + b"\x90\x3c\x50"                                   # note_on C4
    ev += vlq(480) + b"\x80\x3c\x00"                                 # note_off
    ev += vlq(0) + b"\x90\x3e\x50"
    ev += vlq(480) + b"\x80\x3e\x00"
    ev += vlq(0) + b"\xff\x2f\x00"                                   # end_of_track

    raw = (b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big")
           + (1).to_bytes(2, "big") + TPB.to_bytes(2, "big")
           + b"MTrk" + len(ev).to_bytes(4, "big") + bytes(ev))
    p = tmp_path / "cn.mid"
    p.write_bytes(raw)

    pm = pretty_midi.PrettyMIDI(str(p))
    assert pm.instruments, "没读到乐器轨"
    name_read = pm.instruments[0].name
    assert not name_read.isascii(), f"轨名本该带非 ASCII 字符，实际 {name_read!r}"

    info = core.analyze(pm, lo=120.0)
    core.make_piano(pm, info, drop=set(), program=0, mute_drums=True, beat_sec=0.5)
    out = tmp_path / "cn_out.mid"
    pm.write(str(out))          # 这一步以前会抛 UnicodeEncodeError
    assert out.exists() and out.stat().st_size > 0

    # 写出之后轨名必须已是纯 ASCII（否则下次还是写不出去）
    back = pretty_midi.PrettyMIDI(str(out))
    assert all(i.name.isascii() for i in back.instruments), \
        [i.name for i in back.instruments]


# --------------------------------------------------------------------------- #
# 8. 时间/响度辅助
# --------------------------------------------------------------------------- #
def test_hhmmss():
    assert core.hhmmss(0) == "0:00.0"
    assert core.hhmmss(61.5) == "1:01.5"


def test_normalize_loudness_hits_target_and_never_clips():
    sr = 22050
    t = math.pi * 2 * 440 * __import__("numpy").arange(sr) / sr
    x = (0.05 * __import__("numpy").sin(t)).astype("float32")[:, None]
    y, info = core.normalize_loudness(x, sr, target_lufs=-14.0, ceiling=0.92)
    import numpy as np
    assert np.abs(y).max() <= 0.92 + 1e-6
    if "after_lufs" in info:
        assert abs(info["after_lufs"] - (-14.0)) < 1.0, info
