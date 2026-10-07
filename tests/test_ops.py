"""ops（DIY 剪辑）的测试。

重点覆盖那些最容易悄悄出错、但用户一听就会发现的地方：

  * **时间轴换算**：变速曲子（tempo map 有多段）里，"第 10 秒"对应的拍数
    不是常数。算错会让剪辑点整体漂移。
  * **时间边界**：正好落在区间端点上的音符归谁；连续操作后时间轴是否一致。
  * **钳位如实汇报**：移调超出 0–127、音量超出 velocity 127，
    必须钳住并且**把钳了几个报出来**，不能假装完全按用户要求做了。
  * **预设往返**：导出再导入必须一模一样（UI 的导入导出靠它）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pretty_midi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pianoize import ops  # noqa: E402


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def mk(pm_notes, tempo=120.0, n_instruments=1) -> pretty_midi.PrettyMIDI:
    """造一个 PrettyMIDI。pm_notes: [(start, end, pitch, vel)]"""
    pm = pretty_midi.PrettyMIDI(initial_tempo=tempo)
    for i in range(n_instruments):
        ins = pretty_midi.Instrument(program=0, name=f"T{i}")
        for st, en, p, v in pm_notes:
            ins.notes.append(pretty_midi.Note(velocity=v, pitch=p, start=st, end=en))
        pm.instruments.append(ins)
    return pm


def notes_of(pm, inst=0):
    return sorted(((round(n.start, 4), round(n.end, 4), n.pitch, n.velocity)
                   for n in pm.instruments[inst].notes))


def pitches_in(pm, a, b, inst=0):
    return sorted(n.pitch for n in pm.instruments[inst].notes
                  if a - 1e-6 <= n.start < b - 1e-6)


# --------------------------------------------------------------------------- #
# 1. 操作对象本身的校验
# --------------------------------------------------------------------------- #
def test_operation_rejects_bad_input():
    with pytest.raises(ValueError):
        ops.Operation("trim", start=10.0, end=5.0)          # end < start
    with pytest.raises(ValueError):
        ops.Operation("trim", start=5.0, end=5.0)           # 零长度
    with pytest.raises(ValueError):
        ops.Operation("fade", start=0.0, end=1.0)           # 未知类型
    with pytest.raises(ValueError):
        ops.Operation("duplicate", start=0.0, end=1.0, repeat=0)


def test_operation_describe_is_human_readable():
    assert "删除" in ops.Operation("trim", start=1.0, end=2.5).describe()
    d = ops.Operation("pitch", start=0.0, end=1.0, semitones=-12).describe()
    assert "12" in d and "八度" in d
    d = ops.Operation("volume", start=0.0, end=1.0, db=-6).describe()
    assert "dB" in d and "-6" in d


# --------------------------------------------------------------------------- #
# 2. dB 换算
# --------------------------------------------------------------------------- #
def test_db_to_gain_conventions():
    assert ops.db_to_gain(0) == pytest.approx(1.0)
    # 注意：+6 dB 的精确振幅比是 10^(6/20) ≈ 1.9953，不是正好 2.0
    # （正好 2 倍对应 6.0206 dB）。这里按精确值断言，别把近似当恒等。
    assert ops.db_to_gain(6) == pytest.approx(1.99526, rel=1e-4)
    assert ops.db_to_gain(-6) == pytest.approx(0.50119, rel=1e-4)
    assert ops.db_to_gain(20) == pytest.approx(10.0, rel=1e-6)
    assert ops.db_to_gain(0) == 1.0
    # 两种口径的关系：+10 dB 功率 = 10 倍功率，而振幅是它的平方根 ≈ 3.162
    assert ops.db_to_gain(10, "power") == pytest.approx(10.0, rel=1e-9)
    assert ops.db_to_gain(10, "amplitude") == pytest.approx(3.16228, rel=1e-4)
    assert ops.db_to_gain(10, "amplitude") ** 2 == pytest.approx(
        ops.db_to_gain(10, "power"), rel=1e-9)


def test_apply_db_to_velocity_clamps_and_never_zero():
    assert ops.apply_db_to_velocity(64, 0) == 64
    assert ops.apply_db_to_velocity(64, -6) == 32
    assert ops.apply_db_to_velocity(64, 6) == 127            # 128 越界 → 钳到 127
    assert ops.apply_db_to_velocity(100, -100) == ops.VEL_MIN  # 极轻也不为 0
    assert ops.apply_db_to_velocity(1, -30) == ops.VEL_MIN
    assert ops.apply_db_to_velocity(120, 40) == ops.VEL_MAX


# --------------------------------------------------------------------------- #
# 3. tempo map（变速曲子的时间轴换算）
# --------------------------------------------------------------------------- #
def test_tempo_map_constant_tempo():
    pm = mk([(0, 1, 60, 80)], tempo=120.0)
    tm = ops.TempoMap(pm)
    # 120BPM = 每秒 2 拍
    assert tm.sec_to_beat(0.0) == pytest.approx(0.0)
    assert tm.sec_to_beat(1.0) == pytest.approx(2.0)
    assert tm.beat_to_sec(2.0) == pytest.approx(1.0)
    assert tm.beat_to_sec(tm.sec_to_beat(3.7)) == pytest.approx(3.7, abs=1e-6)


def test_tempo_map_with_tempo_change():
    """变速：0s 起 60BPM，10s 起变 120BPM。

    前 10 秒 = 10 拍；之后每秒 2 拍。
    所以第 14 秒 = 10 + 4*2 = 18 拍。
    """
    pm = pretty_midi.PrettyMIDI(initial_tempo=60.0)
    pm._tick_scales = [(0, 60.0 / 60.0 / pm.resolution),
                       (int(10.0 * pm.resolution * 60.0 / 60.0), 60.0 / 120.0 / pm.resolution)]
    ins = pretty_midi.Instrument(program=0)
    ins.notes.append(pretty_midi.Note(80, 60, 0.0, 1.0))
    pm.instruments.append(ins)

    tm = ops.TempoMap(pm)
    assert tm.sec_to_beat(0.0) == pytest.approx(0.0, abs=0.05)
    assert tm.sec_to_beat(10.0) == pytest.approx(10.0, abs=0.05)
    assert tm.sec_to_beat(14.0) == pytest.approx(18.0, abs=0.05)
    # 往返一致
    assert tm.beat_to_sec(tm.sec_to_beat(14.0)) == pytest.approx(14.0, abs=0.05)


# --------------------------------------------------------------------------- #
# 4. trim（删除片段）
# --------------------------------------------------------------------------- #
def test_trim_removes_notes_and_shifts_later_ones():
    # 每 1 秒一个音：0,1,2,3,4
    pm = mk([(float(i), float(i) + 0.5, 60 + i, 80) for i in range(5)])
    before = notes_of(pm)
    ops.apply(pm, [ops.Operation("trim", start=1.0, end=3.0)])
    after = notes_of(pm)
    # 只剩 0、3、4 号（1、2 号在 [1,3) 内被删）
    assert [n[2] for n in after] == [60, 63, 64], after
    # 3、4 号应整体前移 2 秒
    assert after[1][0] == pytest.approx(1.0), after
    assert after[2][0] == pytest.approx(2.0), after
    assert len(before) - len(after) == 2


def test_trim_then_duplicate_uses_new_timeline():
    """操作是依次施加的：先删再复制，复制的时间点应基于删完之后的时间轴。"""
    pm = mk([(float(i), float(i) + 0.5, 60 + i, 80) for i in range(6)])
    ops.apply(pm, [
        ops.Operation("trim", start=0.0, end=2.0),        # 剩 2,3,4,5 → 变 0,1,2,3
        ops.Operation("duplicate", start=0.0, end=1.0),   # 把"第0秒"那个音再播一次
    ])
    starts = sorted(round(n.start, 3) for n in pm.instruments[0].notes)
    # 原始 4 个音在 0,1,2,3；复制第 0 秒那个到 1.0 附近
    assert starts[0] == 0.0
    assert any(abs(s - 1.0) < 1e-3 for s in starts), starts


def test_trim_beyond_end_is_harmless():
    pm = mk([(float(i), float(i) + 0.5, 60, 80) for i in range(3)])
    ops.apply(pm, [ops.Operation("trim", start=100.0, end=110.0)])
    assert len(pm.instruments[0].notes) == 3


# --------------------------------------------------------------------------- #
# 5. duplicate（复制片段）
# --------------------------------------------------------------------------- #
def test_duplicate_keeps_original_and_adds_copies():
    # 0–1s 一个音，2–3s 一个音
    pm = mk([(0.0, 0.5, 60, 80), (2.0, 2.5, 62, 80)])
    _, report = ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=1.0)])
    got = notes_of(pm)
    # 原本 2 个 + 复制 1 个 = 3 个
    assert len(got) == 3, got
    assert [n[2] for n in got] == [60, 60, 62]
    # 复制体应落在 1.0 起（原段长度 1 秒之后）
    assert got[1][0] == pytest.approx(1.0), got
    # 原来的第二个音应被推到 3.0
    assert got[2][0] == pytest.approx(3.0), got
    assert report[0]["复制音符"] == 1


def test_duplicate_multiple_repeats():
    pm = mk([(0.0, 0.5, 60, 80), (5.0, 5.5, 64, 80)])
    _, report = ops.apply(pm,
                          [ops.Operation("duplicate", start=0.0, end=1.0, repeat=3)])
    starts = sorted(round(n.start, 3) for n in pm.instruments[0].notes)
    # 原音 + 3 个复制体（1,2,3 秒）
    assert starts[:4] == [0.0, 1.0, 2.0, 3.0], starts
    # 末尾那个音被推后 3 秒 → 8.0
    assert starts[-1] == pytest.approx(8.0), starts
    assert report[0]["复制音符"] == 3
    assert report[0]["时间轴延长"] == pytest.approx(3.0)


def test_duplicate_preserves_pitch_and_velocity():
    pm = mk([(0.0, 0.5, 67, 90), (3.0, 3.5, 70, 50)])
    ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=1.0)])
    for n in pm.instruments[0].notes:
        if abs(n.start - 1.0) < 1e-3:
            assert n.pitch == 67 and n.velocity == 90


# --------------------------------------------------------------------------- #
# 6. pitch（升降调）
# --------------------------------------------------------------------------- #
def test_pitch_up_and_down_octave():
    pm = mk([(0.0, 0.5, 60, 80), (2.0, 2.5, 64, 80)])
    ops.apply(pm, [ops.Operation("pitch", start=0.0, end=1.0, semitones=12)])
    assert pitches_in(pm, 0.0, 1.0) == [72]
    assert pitches_in(pm, 1.0, 3.0) == [64], "区间外的音不该被动"

    pm2 = mk([(0.0, 0.5, 60, 80), (2.0, 2.5, 64, 80)])
    ops.apply(pm2, [ops.Operation("pitch", start=0.0, end=1.0, semitones=-12)])
    assert pitches_in(pm2, 0.0, 1.0) == [48]


def test_pitch_zero_semitones_is_noop():
    pm = mk([(0.0, 0.5, 60, 80)])
    _, report = ops.apply(pm, [ops.Operation("pitch", start=0.0, end=1.0, semitones=0)])
    assert pm.instruments[0].notes[0].pitch == 60
    assert report[0]["移调"] == 0


def test_pitch_clamps_out_of_range_and_reports_it():
    """音符 120 升 12 半音 → 132，超出 MIDI 上限 127，必须钳住并汇报。"""
    pm = mk([(0.0, 0.5, 120, 80), (0.0, 0.5, 60, 80)])
    _, report = ops.apply(pm, [ops.Operation("pitch", start=0.0, end=1.0, semitones=12)])
    ps = sorted(n.pitch for n in pm.instruments[0].notes)
    assert ps == [72, 127], ps
    assert report[0]["越界被钳"] == 1, report[0]
    assert report[0]["移调音符"] == 2


# --------------------------------------------------------------------------- #
# 7. volume（调音量）
# --------------------------------------------------------------------------- #
def test_volume_applies_db_only_inside_range():
    pm = mk([(0.0, 0.5, 60, 64), (2.0, 2.5, 62, 64)])
    ops.apply(pm, [ops.Operation("volume", start=0.0, end=1.0, db=-6)])
    vels = [(round(n.start, 2), n.velocity) for n in pm.instruments[0].notes]
    assert vels[0][1] == 32, vels        # 64 × 0.5
    assert vels[1][1] == 64, vels        # 区间外不动


def test_volume_reports_saturation():
    """+20dB 想变成 10 倍，但 velocity 上限 127 —— 必须如实报告被钳了几个。

    两个音都会撞上限：100→1000、40→400，都只能记 127。所以是 2 个。
    """
    pm = mk([(0.0, 0.5, 60, 100), (0.1, 0.6, 61, 40)])
    _, report = ops.apply(pm, [ops.Operation("volume", start=0.0, end=1.0, db=20)])
    vels = sorted(n.velocity for n in pm.instruments[0].notes)
    assert vels == [127, 127], vels
    assert report[0]["被钳到极值"] == 2, report[0]
    assert report[0]["改力度音符"] == 2


def test_volume_saturation_not_counted_when_already_at_limit():
    """本来就在 127 的音，被"改回"127 不该算作被钳。"""
    pm = mk([(0.0, 0.5, 60, 127)])
    _, report = ops.apply(pm, [ops.Operation("volume", start=0.0, end=1.0, db=3)])
    assert report[0]["被钳到极值"] == 0, report[0]


def test_volume_never_produces_zero_velocity():
    """velocity 0 在 MIDI 里等于 note off，绝不能产生。"""
    pm = mk([(0.0, 0.5, 60, 10)])
    ops.apply(pm, [ops.Operation("volume", start=0.0, end=1.0, db=-60)])
    assert pm.instruments[0].notes[0].velocity >= ops.VEL_MIN


# --------------------------------------------------------------------------- #
# 8. 组合与顺序
# --------------------------------------------------------------------------- #
def test_order_matters():
    """操作顺序真的会改变结果 —— 用 1 秒处那个音来证明。

    素材：0s=60, 1s=62, 2s=64

    顺序 A：先删 [0,2) 再升调 [0,0.5)
      → 删掉 0s 和 1s 两个音，2s 前移到 0s；此时 [0,0.5) 里是 64 → 升成 76
      → 剩 [76]

    顺序 B：先升调 [0,0.5) 再删 [0,2)
      → 先把 0s 的 60 升成 72，再删掉 0s 和 1s；2s 的 64 前移
      → 剩 [64]（那个被升调的音已经被删了）

    结果不同，正是"按顺序依次施加"这个语义要保证的。
    """
    base = [(0.0, 0.5, 60, 80), (1.0, 1.5, 62, 80), (2.0, 2.5, 64, 80)]

    a = mk(base)
    ops.apply(a, [ops.Operation("trim", start=0.0, end=2.0),
                  ops.Operation("pitch", start=0.0, end=0.5, semitones=12)])
    assert pitches_in(a, 0, 10) == [76], pitches_in(a, 0, 10)

    b = mk(base)
    ops.apply(b, [ops.Operation("pitch", start=0.0, end=0.5, semitones=12),
                  ops.Operation("trim", start=0.0, end=2.0)])
    assert pitches_in(b, 0, 10) == [64], pitches_in(b, 0, 10)

    assert pitches_in(a, 0, 10) != pitches_in(b, 0, 10)


def test_multiple_operations_report_one_row_each():
    pm = mk([(float(i), float(i) + 0.5, 60 + i, 80) for i in range(6)])
    _, report = ops.apply(pm, [
        ops.Operation("volume", start=0.0, end=1.0, db=3),
        ops.Operation("pitch", start=1.0, end=2.0, semitones=1),
        ops.Operation("trim", start=3.0, end=4.0),
        ops.Operation("duplicate", start=0.0, end=0.5),
    ])
    assert len(report) == 4
    assert [r["kind"] for r in report] == ["volume", "pitch", "trim", "duplicate"]
    assert [r["index"] for r in report] == [0, 1, 2, 3]
    for r in report:
        assert r["describe"]


def test_no_negative_times_after_trim():
    """前移操作可能把音符推到负数时间，收尾必须裁掉（否则 MIDI 非法）。"""
    pm = mk([(0.0, 0.5, 60, 80), (1.0, 1.5, 62, 80), (2.0, 2.5, 64, 80)])
    ops.apply(pm, [ops.Operation("trim", start=5.0, end=6.0),
                   ops.Operation("duplicate", start=0.0, end=2.5)])
    for n in pm.instruments[0].notes:
        assert n.start >= 0.0
        assert n.end > n.start


def test_notes_stay_sorted_after_edits():
    pm = mk([(float(i), float(i) + 0.5, 60 + i, 80) for i in range(8)])
    ops.apply(pm, [ops.Operation("duplicate", start=0.0, end=2.0, repeat=2),
                   ops.Operation("trim", start=1.0, end=1.5)])
    starts = [n.start for n in pm.instruments[0].notes]
    assert starts == sorted(starts), "音符没有按时间排序"


# --------------------------------------------------------------------------- #
# 9. 预设 JSON 往返（UI 的导入导出靠它）
# --------------------------------------------------------------------------- #
def test_preset_json_roundtrip():
    p = ops.Preset(
        name="我的方案",
        note="测试",
        operations=[
            ops.Operation("trim", start=10.0, end=20.0),
            ops.Operation("pitch", start=30.0, end=45.0, semitones=-12, label="低八度"),
            ops.Operation("duplicate", start=0.0, end=8.0, repeat=2),
            ops.Operation("volume", start=0.0, end=60.0, db=-6),
        ],
    )
    text = p.to_json()
    back = ops.Preset.from_dict(json.loads(text))
    assert back.name == p.name
    assert len(back.operations) == 4
    for a, b in zip(p.operations, back.operations):
        assert a.to_dict() == b.to_dict()
    # 中文不该被转义
    assert "我的方案" in text


def test_preset_json_roundtrip_via_file(tmp_path):
    p = ops.Preset(name="x", operations=[ops.Operation("trim", start=1.0, end=2.0)])
    f = tmp_path / "preset.json"
    p.to_json(f)
    assert ops.Preset.from_json(f).operations[0].start == 1.0


def test_preset_rejects_garbage():
    with pytest.raises(ValueError):
        ops.Preset.from_dict({"operations": "不是数组"})
    with pytest.raises(ValueError):
        ops.Preset.from_dict({"operations": [{"kind": "bogus"}]})
    with pytest.raises(ValueError):
        ops.Preset.from_dict({"operations": [{"kind": "trim", "start": 1, "end": 2,
                                              "unknown_field": 1}]})


def test_preset_accepts_minimal_operation():
    p = ops.Preset.from_dict({"operations": [{"kind": "trim", "start": 1, "end": 2}]})
    assert p.operations[0].kind == "trim"
    assert p.operations[0].repeat == 1


# --------------------------------------------------------------------------- #
# 10. 用户输入校验（给 UI 的友好警告）
# --------------------------------------------------------------------------- #
def test_validate_warns_on_out_of_range():
    warns = ops.validate_against_duration(
        [ops.Operation("trim", start=100.0, end=110.0)], duration=60.0)
    assert any("超过曲子长度" in w for w in warns)

    warns = ops.validate_against_duration(
        [ops.Operation("trim", start=10.0, end=999.0)], duration=60.0)
    assert any("超出曲子长度" in w for w in warns)


def test_validate_warns_on_noop_and_extreme_ops():
    warns = ops.validate_against_duration(
        [ops.Operation("pitch", start=0.0, end=1.0, semitones=0)], duration=60.0)
    assert any("移调 0 半音" in w for w in warns)

    warns = ops.validate_against_duration(
        [ops.Operation("volume", start=0.0, end=1.0, db=0)], duration=60.0)
    assert any("0 dB" in w for w in warns)

    warns = ops.validate_against_duration(
        [ops.Operation("pitch", start=0.0, end=1.0, semitones=36)], duration=60.0)
    assert any("音域" in w for w in warns)


def test_validate_clean_input_has_no_warnings():
    warns = ops.validate_against_duration(
        [ops.Operation("trim", start=10.0, end=20.0),
         ops.Operation("pitch", start=0.0, end=5.0, semitones=12)], duration=60.0)
    assert warns == []


def test_total_shift_seconds():
    delta = ops.total_shift_seconds([
        ops.Operation("duplicate", start=0.0, end=10.0, repeat=2),   # +20
        ops.Operation("trim", start=0.0, end=5.0),                   # -5
    ])
    assert delta == pytest.approx(15.0)
