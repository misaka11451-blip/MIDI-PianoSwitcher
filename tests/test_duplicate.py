"""duplicate 的插入位置 + 时间轴映射的一致性测试。

对应用户报的两个问题：
  * "复制一段实际会复制两段" —— 这里用精确计数证明只复制了指定的份数
  * "复制的段能选择插在哪里吗" —— 这里覆盖 at 的三种取值
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pretty_midi

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from midi_pianoswitcher import ops  # noqa: E402


def mk_seq(n: int = 20, step: float = 0.5, tempo: float = 120.0,
           repeat_peaks: bool = False):
    """每 step 秒一个不同音高（60,61,62,…），便于精确追踪每个音的来源。"""
    pm = pretty_midi.PrettyMIDI(initial_tempo=tempo)
    ins = pretty_midi.Instrument(program=0, name="Seq")
    for i in range(n):
        t = i * step
        ins.notes.append(pretty_midi.Note(velocity=80, pitch=60 + i,
                                          start=t, end=t + step * 0.9))
    pm.instruments.append(ins)
    return pm


def positions(pm, pitch: int) -> list[float]:
    return sorted(round(n.start, 6) for n in pm.instruments[0].notes
                  if n.pitch == pitch)


def count(pm, pitch: int) -> int:
    return sum(1 for n in pm.instruments[0].notes if n.pitch == pitch)


# --------------------------------------------------------------------------- #
# 复制次数必须精确
# --------------------------------------------------------------------------- #
def test_duplicate_once_adds_exactly_one_copy():
    """repeat=1 → 源区间的每个音**只多出 1 份**（不是两份）。

    这是用户报的"复制一段实际会复制两段"的回归测试。
    """
    pm = mk_seq()
    n_before = len(pm.instruments[0].notes)
    ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=2.0, repeat=1)])
    # 源区间 [0,2) 在 0.5 秒一个音的素材里有 4 个音（60,61,62,63）
    for p in (60, 61, 62, 63):
        assert count(pm, p) == 2, f"音高 {p} 出现 {count(pm, p)} 次，应为 2"
    assert len(pm.instruments[0].notes) == n_before + 4


def test_duplicate_repeat_n_adds_exactly_n_copies():
    for r in (1, 2, 3, 5):
        pm = mk_seq()
        ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=2.0, repeat=r)])
        assert count(pm, 60) == 1 + r, f"repeat={r} → {count(pm, 60)} 次"
        assert count(pm, 63) == 1 + r


def test_duplicate_does_not_overlap_existing_music():
    """副本不能盖住原有音乐：插入点之后的每个音都要整体后移。"""
    pm = mk_seq()
    before = {n.pitch: n.start for n in pm.instruments[0].notes}
    ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=2.0, repeat=1)])
    for p in range(64, 80):          # 源区间之外的音
        assert positions(pm, p) == [pytest.approx(before[p] + 2.0)], \
            f"音高 {p} 没被正确后移"


# --------------------------------------------------------------------------- #
# 插入位置：at
# --------------------------------------------------------------------------- #
def test_duplicate_default_inserts_right_after_source():
    pm = mk_seq()
    ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=2.0, repeat=1)])
    assert positions(pm, 60) == [pytest.approx(0.0), pytest.approx(2.0)]
    assert positions(pm, 63) == [pytest.approx(1.5), pytest.approx(3.5)]


def test_duplicate_at_end():
    """at="end" → 副本落**在原曲内容之后**，且原曲一个音都不动。

    插入点取"所有音符的最大结束时刻"（9.95），不是最后一个音的起点（9.5）。
    用起点的话，"把插入点之后的内容后移" 会把末音自己也推走 —— 原曲就被改了
    （实测：末音从 9.5 被推到 11.5）。所以必须用 end。
    """
    pm = mk_seq()
    end_before = pm.get_end_time()          # 9.95
    ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=2.0,
                                 repeat=1, at="end")])
    pos60 = positions(pm, 60)
    assert pos60[0] == pytest.approx(0.0)
    assert pos60[1] == pytest.approx(end_before)
    # 原曲完全不受影响：末音还待在 9.5
    assert positions(pm, 79) == [pytest.approx(9.5)]
    # 副本那一份要落在原曲之后（音高 60 的两份里，后一份在 9.95）
    assert min(p for p in positions(pm, 60) if p > 1.0) >= end_before - 1e-6
    # 曲子的总结束时刻也相应后移
    assert pm.get_end_time() > end_before


def test_duplicate_at_custom_time():
    """at=5.0 → 副本从 5.0 秒开始，且原本 5.0 秒之后的内容整体后移 2 秒。"""
    pm = mk_seq()
    before = {n.pitch: n.start for n in pm.instruments[0].notes}
    ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=2.0,
                                 repeat=1, at=5.0)])
    assert positions(pm, 60) == [pytest.approx(0.0), pytest.approx(5.0)]
    assert positions(pm, 61) == [pytest.approx(0.5), pytest.approx(5.5)]
    # 原本 >= 5.0 的音后移 2 秒
    for p in range(70, 80):
        assert pytest.approx(before[p] + 2.0) in [pytest.approx(x)
                                                  for x in positions(pm, p)]
    # 原本 < 5.0 的音不动（除了被复制的那些）
    for p in range(64, 70):
        assert before[p] in [pytest.approx(x) for x in positions(pm, p)]


def test_duplicate_at_beginning_of_song():
    """插到 0 秒（曲首）也要正确 —— 这是插入点在源区间**之前**的边界情况。

    第一版实现是"先挪开插入点之后的内容、再复制源区间"，插入点在源区间
    之前时会把源区间自己也搬走，导致复制出重叠的两份。现在改成
    "腾开 → 从副本落点区间取内容 → 复制 → 搬回"，任何插入点都成立。
    """
    pm = mk_seq()
    ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=2.0,
                                 repeat=1, at=0.0)])
    # 每个源音恰好 2 份，不多不少
    assert count(pm, 60) == 2, positions(pm, 60)
    assert count(pm, 63) == 2, positions(pm, 63)
    # 副本落在 [0,2)
    assert positions(pm, 60) == [pytest.approx(0.0), pytest.approx(2.0)]
    # 原本的内容整体后移 2 秒：原 2.0s 的音高 64 现在在 4.0s
    assert positions(pm, 64) == [pytest.approx(4.0)]
    # 总音数 = 原有 20 + 副本 4
    assert len(pm.instruments[0].notes) == 24


def test_duplicate_repeat_with_at_end_stacks():
    """at="end" + repeat=3 → 三份副本依次排在原曲之后（间隔 = 区间长度）。"""
    pm = mk_seq()
    end = pm.get_end_time()          # 9.95 = 所有音符的最大结束时刻
    ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=2.0,
                                 repeat=3, at="end")])
    pos60 = positions(pm, 60)
    assert pos60[0] == pytest.approx(0.0)
    assert pos60[1:] == [pytest.approx(end), pytest.approx(end + 2.0),
                         pytest.approx(end + 4.0)]


def test_duplicate_total_length():
    """总时长增加量 = 区间长度 × repeat，与插入位置无关。"""
    for at in (None, "end", 5.0, 0.0):
        pm = mk_seq()
        base = pm.get_end_time()          # 9.95
        ops.apply(pm, [ops.Operation("duplicate", start=1.0, end=3.0,
                                     repeat=2, at=at)])
        # 加了 2×2=4 秒
        assert pm.get_end_time() == pytest.approx(base + 4.0, abs=0.05), \
            (at, pm.get_end_time(), base)


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
def test_at_only_for_duplicate():
    with pytest.raises(ValueError, match="复制"):
        ops.Operation("trim", start=0.0, end=1.0, at=2.0)


def test_at_rejects_invalid_values():
    with pytest.raises(ValueError, match="end"):
        ops.Operation("duplicate", start=0.0, end=1.0, at="middle")
    with pytest.raises(ValueError, match="负数"):
        ops.Operation("duplicate", start=0.0, end=1.0, at=-1.0)


def test_at_accepts_string_number():
    op = ops.Operation("duplicate", start=0.0, end=1.0, at="3.5")
    assert op.at == 3.5


def test_insert_desc_is_readable():
    assert ops.Operation("duplicate", start=0, end=1).insert_desc() == "紧跟其后"
    assert ops.Operation("duplicate", start=0, end=1,
                         at="end").insert_desc() == "曲末"
    assert "5.00" in ops.Operation("duplicate", start=0, end=1,
                                   at=5.0).insert_desc()
    assert "曲末" in ops.Operation("duplicate", start=0, end=1,
                                  at="end").describe()


# --------------------------------------------------------------------------- #
# 时间轴映射必须跟着 at 走
# --------------------------------------------------------------------------- #
def test_timeline_map_respects_at_end():
    oper = [ops.Operation("duplicate", start=0.0, end=2.0, repeat=1, at="end")]
    m = ops.timeline_map(oper, 10.0)
    assert m
    total_out = sum(s["out_end"] - s["out_start"] for s in m)
    assert total_out == pytest.approx(12.0, abs=1e-6), (total_out, m)
    # 副本段应该落在末尾 [10,12)
    tail = [s for s in m if s["out_start"] >= 9.99]
    assert tail, m
    assert tail[0]["src_start"] == pytest.approx(0.0)
    assert tail[0]["src_end"] == pytest.approx(2.0)


def test_timeline_map_respects_at_custom():
    oper = [ops.Operation("duplicate", start=0.0, end=2.0, repeat=1, at=5.0)]
    m = ops.timeline_map(oper, 10.0)
    assert m
    total_out = sum(s["out_end"] - s["out_start"] for s in m)
    assert total_out == pytest.approx(12.0, abs=1e-6)
    # 应该有一段 out 落在 [5,7) 且源是 [0,2)
    mid = [s for s in m if abs(s["out_start"] - 5.0) < 1e-6]
    assert mid, m
    assert mid[0]["src_start"] == pytest.approx(0.0)
    assert mid[0]["src_end"] == pytest.approx(2.0)


def test_timeline_map_no_overlap_with_at():
    """任何插入位置下，映射出来的段都不能互相重叠。"""
    for at in (None, "end", 3.0):
        oper = [ops.Operation("duplicate", start=1.0, end=2.5,
                              repeat=2, at=at)]
        m = ops.timeline_map(oper, 10.0)
        ordered = sorted((s["out_start"], s["out_end"]) for s in m)
        for a, b in zip(ordered, ordered[1:]):
            assert b[0] >= a[1] - 1e-6, (at, a, b)


def test_timeline_map_at_zero_covers_whole_output():
    """插到曲首时，段必须首尾相接覆盖整个输出时间轴（否则播放头会跳）。"""
    oper = [ops.Operation("duplicate", start=1.0, end=2.5, repeat=1, at=0.0)]
    m = ops.timeline_map(oper, 10.0)
    assert m
    ordered = sorted((s["out_start"], s["out_end"]) for s in m)
    assert ordered[0][0] == pytest.approx(0.0, abs=1e-6)
    assert ordered[-1][1] == pytest.approx(11.5, abs=1e-6), ordered[-1]
    for a, b in zip(ordered, ordered[1:]):
        assert b[0] == pytest.approx(a[1], abs=1e-6), (a, b)


# --------------------------------------------------------------------------- #
# 预设往返要带上 at
# --------------------------------------------------------------------------- #
def test_preset_roundtrip_keeps_at():
    p = ops.Preset(name="x", operations=[
        ops.Operation("duplicate", start=0.0, end=2.0, repeat=2, at="end"),
        ops.Operation("duplicate", start=1.0, end=2.0, at=7.5),
        ops.Operation("duplicate", start=0.0, end=1.0),
    ])
    back = ops.Preset.from_dict(__import__("json").loads(p.to_json()))
    assert back.operations[0].at == "end"
    assert back.operations[1].at == pytest.approx(7.5)
    assert back.operations[2].at is None


def test_payload_omits_at_when_default():
    """默认插入位置不该往 JSON 里塞 at 字段（保持预设干净）。"""
    d = ops.Operation("duplicate", start=0.0, end=1.0).to_dict()
    assert "at" not in d
    d2 = ops.Operation("duplicate", start=0.0, end=1.0, at="end").to_dict()
    assert d2["at"] == "end"
