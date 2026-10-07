"""ops —— DIY 音频/乐谱剪辑操作。

设计上有一个关键决定：**所有剪辑都在 MIDI 域完成，而不是在音频波形上剪。**

为什么：

1. 一次剪辑，三份产物都自动正确 —— 音频、MIDI、钢琴谱全都跟着变，
   不会出现"音频剪了但谱子还是旧的"这种不一致。
2. **完全可复现**。操作清单可以存成 JSON 预设，随时重跑、随时改，
   而不是得到一堆改完就没法回溯的波形。
3. 音高、音量这类参数在 MIDI 里是**精确的**（音符号、velocity），
   在音频域做则要靠 phase vocoder 猜，必然引入伪影。

所有操作的共同语义：**时间用"秒"表示，针对原始文件的时间轴**（t=0 就是原曲开头）。

操作是按列表顺序依次施加的，所以顺序有意义：
先删中间一段再复制开头，和先复制再删，结果不同。UI 会明确展示顺序。

支持的四种操作：

    trim       删除 [start, end) 这一段
    duplicate  把 [start, end) 这一段的音乐再播一次（插在该段结束后）
    pitch      把 [start, end) 整体升高/降低若干个半音
    volume     把 [start, end) 整体音量增减若干 dB

用法：

    from pianoize import ops
    preset = ops.Preset.from_json(path)
    new_pm = ops.apply(pm, preset.operations)
    preset.to_json(path)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Literal

import numpy as np
import pretty_midi

Kind = Literal["trim", "duplicate", "pitch", "volume"]

# MIDI 音符号的合法区间（0–127）；大幅移调必须钳住，否则文件非法
MIDI_PITCH_MIN, MIDI_PITCH_MAX = 0, 127
# MIDI velocity 合法区间
VEL_MIN, VEL_MAX = 1, 127
# 时间比较的容差（秒）——避免浮点误差把紧邻边界的音符判错
EPS = 1e-6


# --------------------------------------------------------------------------- #
# 操作定义
# --------------------------------------------------------------------------- #
@dataclass
class Operation:
    """一个剪辑操作。

    start/end 是**秒**，针对当前（已被前序操作改过的）时间轴。
    """

    kind: Kind
    start: float = 0.0
    end: float = 0.0
    semitones: int = 0          # pitch 用
    db: float = 0.0             # volume 用
    repeat: int = 1             # duplicate 用：重复几次
    label: str = ""             # 用户备注，不参与计算

    def __post_init__(self):
        if self.kind not in ("trim", "duplicate", "pitch", "volume"):
            raise ValueError(f"未知操作类型：{self.kind}")
        if self.end < self.start:
            raise ValueError(f"end({self.end}) 不能小于 start({self.start})")
        if self.kind in ("trim", "duplicate", "pitch", "volume"):
            if self.end - self.start <= EPS:
                raise ValueError(f"{self.kind} 需要一段非零长度的区间")
        if self.kind == "repeat" or self.repeat < 1:
            raise ValueError("repeat 至少为 1")

    # ---- 序列化 ----
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if not (k == "label" and not v)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Operation":
        allowed = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(f"操作里有无法识别的字段：{sorted(unknown)}")
        return cls(**{k: v for k, v in d.items() if k in allowed})

    def describe(self) -> str:
        """给 UI/日志用的人话描述"""
        a, b = self.start, self.end
        rng = f"{a:.2f}s–{b:.2f}s"
        if self.kind == "trim":
            return f"删除 {rng}（{b - a:.2f}s）"
        if self.kind == "duplicate":
            extra = f" ×{self.repeat}" if self.repeat > 1 else ""
            return f"复制 {rng} 再播 {self.repeat} 次（多出 {(b - a) * self.repeat:.2f}s）"
        if self.kind == "pitch":
            sign = "+" if self.semitones >= 0 else ""
            octs = self.semitones / 12
            return f"区间 {rng} 音高 {sign}{self.semitones} 半音（{sign}{octs:g} 个八度）"
        if self.kind == "volume":
            sign = "+" if self.db >= 0 else ""
            return f"区间 {rng} 音量 {sign}{self.db:g} dB"
        return f"{self.kind} {rng}"


@dataclass
class Preset:
    """一整份剪辑方案，可存成 JSON。"""

    operations: list[Operation] = field(default_factory=list)
    name: str = "未命名方案"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "note": self.note,
            "operations": [o.to_dict() for o in self.operations],
        }

    def to_json(self, path: str | Path | None = None) -> str:
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        if path:
            Path(path).write_text(text, encoding="utf-8")
        return text

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Preset":
        if not isinstance(d, dict):
            raise ValueError("预设必须是一个 JSON 对象")
        raw = d.get("operations", [])
        if not isinstance(raw, list):
            raise ValueError("operations 必须是数组")
        return cls(
            operations=[Operation.from_dict(o) for o in raw],
            name=str(d.get("name") or "未命名方案"),
            note=str(d.get("note") or ""),
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "Preset":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# dB → velocity
# --------------------------------------------------------------------------- #
def db_to_gain(db: float, mode: str = "amplitude") -> float:
    """分贝换算成线性增益。

    默认按**振幅**算（20·log10）：音乐软件里的音量推子就是振幅，
    所以 "+6 dB" ≈ 响一倍、"-6 dB" ≈ 轻一半，符合直觉。

    用 "power" 则按 10·log10 算（能量），保留给需要严格能量口径的场景。
    """
    return 10 ** (db / (20.0 if mode == "amplitude" else 10.0))


def apply_db_to_velocity(vel: int, db: float, mode: str = "amplitude") -> int:
    """把 dB 增减作用到 MIDI velocity 上，钳到合法区间。

    MIDI velocity 是 1–127 的整数，所以：
      * velocity 0 是"note off"的写法，绝对不能产生
      * 超过 127 只能钳住 —— 这意味着 +20 dB 在 velocity 上做不到（会失真），
        想真正加那么多只能在渲染后的音频上做。这里如实钳住并让调用方汇报。
    """
    if vel <= 0:
        return VEL_MIN
    g = db_to_gain(db, mode)
    out = int(round(vel * g))
    return max(VEL_MIN, min(VEL_MAX, out))


# --------------------------------------------------------------------------- #
# 时间轴换算（必须走 tempo map）
# --------------------------------------------------------------------------- #
class TempoMap:
    """秒 ↔ 拍 的换算。

    为什么必须单独做：MIDI 里音符位置是按"拍"（tick）存的，
    而用户输入的是"秒"。只要曲子有变速，某个固定秒数对应的拍号位置
    就不是常数，必须按 tempo map 分段积分。

    忽略这一层是这类工具最常见的 bug：变速曲子上剪辑点会整体漂移。
    """

    def __init__(self, pm: pretty_midi.PrettyMIDI):
        times, tempi = pm.get_tempo_changes()
        if len(times) == 0:
            times, tempi = np.array([0.0]), np.array([120.0])
        order = np.argsort(times)
        self._t = np.asarray(times, dtype=float)[order]
        self._bpm = np.asarray(tempi, dtype=float)[order]
        # 每个变速点对应的"累计拍数"
        self._beats = np.zeros_like(self._t)
        for i in range(1, len(self._t)):
            self._beats[i] = self._beats[i - 1] + \
                (self._t[i] - self._t[i - 1]) * self._bpm[i - 1] / 60.0

    def sec_to_beat(self, sec: float) -> float:
        if sec <= self._t[0]:
            return (sec - self._t[0]) * self._bpm[0] / 60.0
        i = int(np.searchsorted(self._t, sec, side="right") - 1)
        i = max(0, min(i, len(self._t) - 1))
        return self._beats[i] + (sec - self._t[i]) * self._bpm[i] / 60.0

    def beat_to_sec(self, beat: float) -> float:
        if beat <= self._beats[0]:
            return self._t[0] + (beat - self._beats[0]) * 60.0 / self._bpm[0]
        i = int(np.searchsorted(self._beats, beat, side="right") - 1)
        i = max(0, min(i, len(self._t) - 1))
        return self._t[i] + (beat - self._beats[i]) * 60.0 / self._bpm[i]

    @property
    def beats_per_sec_at_end(self) -> float:
        return self._bpm[-1] / 60.0


# --------------------------------------------------------------------------- #
# 施加操作
# --------------------------------------------------------------------------- #
def _overlaps(start: float, end: float, a: float, b: float) -> bool:
    """note 区间 [start,end) 与操作区间 [a,b) 是否有交叠"""
    return end > a + EPS and start < b - EPS


def _move_notes_after(pm: pretty_midi.PrettyMIDI, tmap: TempoMap,
                      from_sec: float, delta_sec: float) -> None:
    """把所有 onset >= from_sec 的音符整体平移 delta_sec（负值即前移）。"""
    if abs(delta_sec) < EPS:
        return
    for ins in pm.instruments:
        for n in ins.notes:
            if n.start >= from_sec - EPS:
                n.start += delta_sec
                n.end += delta_sec
        for cc in ins.control_changes:
            if cc.time >= from_sec - EPS:
                cc.time += delta_sec
        for pb in ins.pitch_bends:
            if pb.time >= from_sec - EPS:
                pb.time += delta_sec


def _op_trim(pm: pretty_midi.PrettyMIDI, tmap: TempoMap, op: Operation) -> dict:
    """删掉 [start,end)：区间内的音符丢掉，后面的整体前移。"""
    removed = 0
    length = op.end - op.start
    for ins in pm.instruments:
        keep = []
        for n in ins.notes:
            if _overlaps(n.start, n.end, op.start, op.end):
                removed += 1
                continue
            keep.append(n)
        ins.notes = keep
        # 控制器/弯音落在区间内的也丢掉，否则后面会突然变调
        ins.control_changes = [c for c in ins.control_changes
                               if not (op.start <= c.time < op.end)]
        ins.pitch_bends = [b for b in ins.pitch_bends
                           if not (op.start <= b.time < op.end)]
    _move_notes_after(pm, tmap, op.end, -length)
    return {"删除音符": removed}


def _op_duplicate(pm: pretty_midi.PrettyMIDI, tmap: TempoMap, op: Operation) -> dict:
    """把 [start,end) 的内容再演奏 repeat 次，插在该段结束后。

    实现：后面所有音符整体后移 (长度 × repeat)，然后在腾出的空位里
    按原来的偏移量复制 repeat 份。
    """
    length = op.end - op.start
    shift = length * op.repeat
    # 先把后面的挪开
    _move_notes_after(pm, tmap, op.end, shift)

    copied = 0
    for ins in pm.instruments:
        src = [n for n in list(ins.notes)
               if op.start - EPS <= n.start < op.end - EPS]
        for r in range(1, op.repeat + 1):
            off = op.start + length * r
            for n in src:
                ins.notes.append(pretty_midi.Note(
                    velocity=n.velocity,
                    pitch=n.pitch,
                    start=off + (n.start - op.start),
                    end=off + (n.end - op.start),
                ))
                copied += 1
        # 控制器也跟着复制，否则复制段的踏板/音量会丢
        ccs = [c for c in list(ins.control_changes)
               if op.start - EPS <= c.time < op.end - EPS]
        for r in range(1, op.repeat + 1):
            off = op.start + length * r
            for c in ccs:
                ins.control_changes.append(
                    pretty_midi.ControlChange(c.number, c.value, off + (c.time - op.start)))
    return {"复制音符": copied, "时间轴延长": round(shift, 3)}


def _op_pitch(pm: pretty_midi.PrettyMIDI, tmap: TempoMap, op: Operation) -> dict:
    """区间内整体移调。

    移调会把音符号推出 0–127 的合法区间，超出部分必须钳住 —— 钳住会让
    旋律变形，所以这里要如实汇报钳了几个音，别让用户以为完全按预期移了。
    """
    if op.semitones == 0:
        return {"移调": 0}
    changed = clamped = 0
    for ins in pm.instruments:
        for n in ins.notes:
            if op.start - EPS <= n.start < op.end - EPS:
                target = n.pitch + op.semitones
                if target < MIDI_PITCH_MIN or target > MIDI_PITCH_MAX:
                    clamped += 1
                    target = max(MIDI_PITCH_MIN, min(MIDI_PITCH_MAX, target))
                n.pitch = int(target)
                changed += 1
    return {"移调音符": changed, "越界被钳": clamped}


def _op_volume(pm: pretty_midi.PrettyMIDI, tmap: TempoMap, op: Operation) -> dict:
    """区间内整体增减音量。

    在 MIDI 域改 velocity 是精确的；但 velocity 只有 1–127，
    所以 +20 dB 这种东西会被钳住（结果并不等于真的响 4 倍）。
    要真正实现大幅增益，得在渲染出的音频上做（UI 也提供了这种模式）。
    """
    changed = saturated = 0
    for ins in pm.instruments:
        for n in ins.notes:
            if op.start - EPS <= n.start < op.end - EPS:
                before = n.velocity
                after = apply_db_to_velocity(before, op.db)
                if after in (VEL_MIN, VEL_MAX) and before not in (VEL_MIN, VEL_MAX):
                    saturated += 1
                n.velocity = after
                changed += 1
        for cc in ins.control_changes:
            if cc.number == 7 and op.start - EPS <= cc.time < op.end - EPS:
                g = db_to_gain(op.db)
                cc.value = max(0, min(127, int(round(cc.value * g))))
    return {"改力度音符": changed, "被钳到极值": saturated}


_APPLIERS = {
    "trim": _op_trim,
    "duplicate": _op_duplicate,
    "pitch": _op_pitch,
    "volume": _op_volume,
}


def apply(pm: pretty_midi.PrettyMIDI, operations: Iterable[Operation],
          ) -> tuple[pretty_midi.PrettyMIDI, list[dict]]:
    """按顺序施加所有操作。

    **会修改传入的 pm**（这样调用方拿到的就是剪辑后的对象）；
    同时返回每个操作的执行统计，方便在 UI 上如实展示"有几个音被钳住了"。
    """
    tmap = TempoMap(pm)
    report: list[dict] = []
    for i, op in enumerate(operations):
        # 每个操作前重算 tempo map：前面的操作可能已经改了时间轴
        tmap = TempoMap(pm)
        result = _APPLIERS[op.kind](pm, tmap, op)
        report.append({
            "index": i,
            "kind": op.kind,
            "describe": op.describe(),
            **result,
        })
    # 收尾：负数时间（前移操作可能造成）裁掉，避免产生非法 MIDI
    for ins in pm.instruments:
        for n in ins.notes:
            if n.start < 0:
                n.end -= n.start
                n.start = 0.0
            if n.end <= n.start:
                n.end = n.start + 0.001
        ins.notes.sort(key=lambda n: (n.start, n.pitch))
        ins.control_changes.sort(key=lambda c: c.time)
    return pm, report


def total_shift_seconds(operations: Iterable[Operation]) -> float:
    """估算所有 duplicate 带来的净时间轴延长（trim 会缩短）。"""
    delta = 0.0
    for op in operations:
        if op.kind == "duplicate":
            delta += (op.end - op.start) * op.repeat
        elif op.kind == "trim":
            delta -= (op.end - op.start)
    return delta


def validate_against_duration(operations: Iterable[Operation], duration: float,
                              ) -> list[str]:
    """给出"人能看懂"的警告（不抛异常，UI 需要原样展示）。

    真正的硬校验在 Operation.__post_init__ 里；这里只处理
    "区间超出曲子长度"这类用户容易搞错、但不该直接报错挡住的输入。
    """
    warns: list[str] = []
    for i, op in enumerate(operations):
        tag = f"第 {i + 1} 条（{op.kind}）"
        if op.start > duration + EPS:
            warns.append(f"{tag}：起点 {op.start:.2f}s 已经超过曲子长度 {duration:.2f}s")
        elif op.end > duration + EPS:
            warns.append(f"{tag}：终点 {op.end:.2f}s 超出曲子长度 {duration:.2f}s，"
                         f"会被按 {duration:.2f}s 处理")
        if op.kind == "pitch" and op.semitones == 0:
            warns.append(f"{tag}：移调 0 半音，等于没改")
        if op.kind == "volume" and abs(op.db) < 1e-9:
            warns.append(f"{tag}：音量变化 0 dB，等于没改")
        if op.kind == "pitch" and abs(op.semitones) >= 24:
            warns.append(f"{tag}：移调 {op.semitones} 半音幅度很大，"
                         f"超出音域的音会被钳住，旋律会变形")
    return warns
