"""验证 ops.timeline_map —— 原始时间轴 ↔ 处理后时间轴 的映射。

为什么单独测：界面上的播放头靠它对齐（波形是原始时间轴，试听音频是处理后
的时间轴）。这类几何逻辑不对着具体数字测一遍，几乎不可能写对。

验证方式：用**独立复算**的办法——按"操作对时间轴的直觉语义"手算出每一段的
落点，再和 timeline_map 的输出逐段比对。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from midi_pianoswitcher import ops  # noqa: E402


def span(seg) -> tuple[float, float, float, float]:
    return (seg["out_start"], seg["out_end"], seg["src_start"], seg["src_end"])


def test_no_timeline_ops_returns_empty():
    """pitch / volume 不改时间轴，应该返回空（前端据此退回比例映射）。"""
    m = ops.timeline_map([ops.Operation("pitch", start=0, end=5, semitones=12),
                          ops.Operation("volume", start=0, end=5, db=-3)],
                         duration=60.0)
    assert m == []


def test_trim_interior():
    """删 [10,20)，曲长 60。

    直觉：剩下 [0,10) 不动；[20,60) 前移 10 秒 → 落在 [10,50)。
    """
    m = ops.timeline_map([ops.Operation("trim", start=10.0, end=20.0)], 60.0)
    assert [span(s) for s in m] == [
        (0.0, 10.0, 0.0, 10.0),
        (10.0, 50.0, 20.0, 60.0),
    ]


def test_trim_from_zero():
    """删 [0,10) → 剩下 [10,60) 前移 10 → 落在 [0,50)。"""
    m = ops.timeline_map([ops.Operation("trim", start=0.0, end=10.0)], 60.0)
    assert [span(s) for s in m] == [(0.0, 50.0, 10.0, 60.0)]


def test_trim_at_end():
    """删 [50,60) → 前面 [0,50) 完全不动。"""
    m = ops.timeline_map([ops.Operation("trim", start=50.0, end=60.0)], 60.0)
    assert [span(s) for s in m] == [(0.0, 50.0, 0.0, 50.0)]


def test_duplicate_interior():
    """把 [10,20)（长 10）再播 1 次，插在 20 处，曲长 60。

    直觉：原内容的落点按"在不在 [a,b) 之后"整体判断会错，因为副本是**插在**
    区间内部。正确结果：
      · [0,10)  不动            → out [0,10)
      · [10,20) 不动（本身就是要复制的那段） → out [10,20)
      · [20,60) 后移 10         → out [30,70)
      · 副本（源 [10,20)）       → out [20,30)
    """
    m = ops.timeline_map(
        [ops.Operation("duplicate", start=10.0, end=20.0, repeat=1)], 60.0)
    got = sorted(span(s) for s in m)
    assert got == [
        (0.0, 10.0, 0.0, 10.0),
        (10.0, 20.0, 10.0, 20.0),
        (20.0, 30.0, 10.0, 20.0),      # 副本
        (30.0, 70.0, 20.0, 60.0),      # 原本在区间之后的内容被推后
    ], got


def test_duplicate_repeat_twice():
    """repeat=2 → 副本落在 [20,30) 和 [30,40)，后面内容后移 20。"""
    m = ops.timeline_map(
        [ops.Operation("duplicate", start=10.0, end=20.0, repeat=2)], 60.0)
    got = sorted(span(s) for s in m)
    assert got == [
        (0.0, 10.0, 0.0, 10.0),
        (10.0, 20.0, 10.0, 20.0),
        (20.0, 30.0, 10.0, 20.0),
        (30.0, 40.0, 10.0, 20.0),
        (40.0, 80.0, 20.0, 60.0),
    ], got


def test_duplicate_at_start():
    """把开头 [0,10) 再播 1 次 → 副本在 [10,20)，其余后移 10。"""
    m = ops.timeline_map(
        [ops.Operation("duplicate", start=0.0, end=10.0, repeat=1)], 60.0)
    got = sorted(span(s) for s in m)
    assert got == [
        (0.0, 10.0, 0.0, 10.0),
        (10.0, 20.0, 0.0, 10.0),       # 副本
        (20.0, 70.0, 10.0, 60.0),
    ], got


def test_trim_then_duplicate():
    """先删 [10,20) 再复制 [0,10)。

    删完曲长 50：原 [0,10) 仍在 [0,10)，原 [20,60) 在 [10,50)。
    再复制 [0,10)（repeat=1）→ 副本插在 10 处，其余后移 10：
      · [0,10)   → out [0,10)
      · 副本      → out [10,20)
      · 原 [20,60) 现在在 [10,50)，被推后到 [20,60)
    """
    m = ops.timeline_map([
        ops.Operation("trim", start=10.0, end=20.0),
        ops.Operation("duplicate", start=0.0, end=10.0, repeat=1),
    ], 60.0)
    got = sorted(span(s) for s in m)
    assert got == [
        (0.0, 10.0, 0.0, 10.0),
        (10.0, 20.0, 0.0, 10.0),
        (20.0, 60.0, 20.0, 60.0),
    ], got


def test_duplicate_then_trim():
    """先复制 [0,10) 再删 [30,40)（注意 30–40 落在被推后之后的区域）。

    复制后：原 [0,10) 在 [0,10)，副本在 [10,20)，原 [10,60) 在 [20,70)。
    此时删 [30,40)（处理后时间轴）…… 但我们的操作是针对**当前**时间轴的，
    timeline_map 也按同样语义处理：删掉后 max over 40 之后的落点前移 10。
    """
    m = ops.timeline_map([
        ops.Operation("duplicate", start=0.0, end=10.0, repeat=1),
        ops.Operation("trim", start=30.0, end=40.0),
    ], 60.0)
    # 关键性质：所有段的 out 区间必须互不重叠，且顺次拼接后总长 = 期望时长
    total_out = sum(s["out_end"] - s["out_start"] for s in m)
    # 净变换：+10（复制） -10（删除） = 0
    assert total_out == pytest.approx(60.0, abs=1e-6), (total_out, [span(s) for s in m])
    # 落点区间不该重叠
    ordered = sorted(span(s) for s in m)
    for a, b in zip(ordered, ordered[1:]):
        assert b[0] >= a[1] - 1e-6, (a, b)


def test_segments_are_contiguous_and_cover_output():
    """所有段按 out 排序后应该首尾相接、不留缝（否则播放头会跳）。"""
    m = ops.timeline_map([
        ops.Operation("trim", start=5.0, end=9.0),
        ops.Operation("duplicate", start=0.0, end=3.0, repeat=2),
        ops.Operation("trim", start=20.0, end=22.0),
    ], 40.0)
    assert m
    ordered = sorted(span(s) for s in m)
    assert ordered[0][0] == pytest.approx(0.0, abs=1e-6)
    for a, b in zip(ordered, ordered[1:]):
        assert b[0] == pytest.approx(a[1], abs=1e-6), (a, b)


def test_total_output_length_matches_prediction():
    """所有段的 out 总长 = 原时长 - trim 总长 + duplicate 总延长。"""
    oper = [
        ops.Operation("trim", start=10.0, end=20.0),               # -10
        ops.Operation("duplicate", start=0.0, end=5.0, repeat=3),  # +15
    ]
    m = ops.timeline_map(oper, 60.0)
    total_out = sum(s["out_end"] - s["out_start"] for s in m)
    expected = 60.0 + ops.total_shift_seconds(oper)
    assert total_out == pytest.approx(expected, abs=1e-6), (total_out, expected)


def test_too_many_segments_falls_back_to_empty():
    """操作太碎时返回空列表，让前端退回比例映射（而不是给出错误映射）。"""
    oper = [ops.Operation("trim", start=float(i), end=float(i) + 0.1)
            for i in range(0, 200, 2)]
    assert ops.timeline_map(oper, 200.0, max_segments=10) == []


def test_timeline_map_is_json_serializable():
    import json
    m = ops.timeline_map([ops.Operation("trim", start=1.0, end=2.0)], 10.0)
    text = json.dumps(m)
    assert json.loads(text) == m
