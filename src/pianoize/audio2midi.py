"""音频 → MIDI 的多音高转录（轻量、无需深度学习依赖）。

⚠️ 先读这段再决定要不要用：

音频转 MIDI 是一个**信息有损**的问题。波形里没有"这是什么乐器、这个音多长、
力度多少、几几拍"这些信息，全靠推测。所以：

  * 单乐器独奏（尤其钢琴）→ 还能用，但错音、漏音、多音是常态
  * 完整混音（有鼓、有贝斯、有合成器垫）→ 基本不可用，别指望

本项目实测（用合成 MIDI 渲染成音频再转回来，等于"上限测试"）：
纯钢琴素材上，朴素频谱法的 F1 ≈ 0.11。

**如果你的目标是"纯钢琴版"，最优解永远是找到原曲的 MIDI，而不是从音频转。**

这个模块提供的是"没有 MIDI 时的兜底"：频谱峰值 → 音高 → 按帧聚类成音符
→ 输出 MIDI。用户可以拿它当草稿，再人工修。

用法：

    python -m pianoize.audio2midi --src song.mp3 --out song.mid
    python -m pianoize.audio2midi --src song.mp3 --out song.mid --thresh 0.15 --min-len 0.08
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 只保留钢琴音域（A0–C8），避免把低频鼓点和高频镲片转成荒谬的音
PIANO_LOW, PIANO_HIGH = 21, 108


def _ffmpeg_decode(path: Path, sr: int) -> tuple[np.ndarray, int]:
    """用 ffmpeg 把任意格式解成 wav 再读。

    为什么需要：libsndfile **不认 m4a/aac**（实测报 "Format not recognised"），
    而 m4a 是从各种平台下载音频的常见格式。ffmpeg 认得几乎所有格式，
    所以它是兜底路径。没有 ffmpeg 时给出可操作的提示。
    """
    import shutil
    import tempfile
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError(
            f"读不了 {path.name}：当前解码器不支持这个格式，且系统里找不到 ffmpeg。\n"
            f"  解决：装一个 ffmpeg（winget install ffmpeg / brew install ffmpeg），\n"
            f"       或者先把文件转成 wav/mp3 再来。")
    with tempfile.TemporaryDirectory(prefix="pianoize_a2m_") as td:
        wav = Path(td) / "decoded.wav"
        r = subprocess.run(
            [ff, "-y", "-v", "error", "-i", str(path),
             "-ac", "1", "-ar", str(sr), "-f", "wav", str(wav)],
            capture_output=True)
        if r.returncode != 0 or not wav.exists():
            msg = (r.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            raise RuntimeError(f"ffmpeg 解码失败（返回码 {r.returncode}）：{msg}")
        import soundfile as sf
        y, got_sr = sf.read(str(wav), dtype="float32", always_2d=False)
        if y.ndim > 1:
            y = y.mean(axis=1)
        return y, got_sr


def load_audio(path: Path, sr: int = 22050) -> tuple[np.ndarray, int]:
    """读任何音频格式。优先 librosa/soundfile；不行就交给 ffmpeg 兜底。"""
    import warnings

    import librosa
    try:
        y, got_sr = librosa.load(str(path), sr=sr, mono=True)
        return y, got_sr
    except Exception as e:
        first = f"{type(e).__name__}: {str(e)[:120]}"
    warnings.warn(f"librosa 直接读取失败（{first}），改用 ffmpeg 解码。")
    return _ffmpeg_decode(Path(path), sr)


def spectral_peaks(y: np.ndarray, sr: int, hop: int = 512, n_fft: int = 4096,
                   max_poly: int = 6, bps: int = 1, harmonic_suppress: bool = True):
    """逐帧找频谱峰值 → 音高。

    返回 (帧时刻数组, 每帧的音高列表, 每帧的能量列表, CQT 矩阵)。

    用 CQT 把频谱映射到对数音高轴。参数换算容易搞错，写清楚：

      * librosa 的 ``bins_per_octave`` 是**每个八度的 bin 数**，
        而一个八度有 12 个半音 → 想要"每个半音 bps 个 bin"，
        就得传 ``bins_per_octave = 12 * bps``。
      * ``n_bins`` 则是总 bin 数 = 半音数 * bps。
        传小了覆盖不到高音；传大了上界会超过 Nyquist 直接报错
        （踩过：把 math.ceil 的结果再乘一遍，上界冲到 1e27 Hz）。

    关于 ``harmonic_suppress``：
      钢琴一个音会同时产生很强的泛音列（八度、十二度、双八度…）。
      CQT 里这些泛音本身就是独立峰值，直接峰值挑音会让每个音都
      多出好几个"幽灵音"（实测误报 1139 个，精确率只有 27%）。
      所以这里把"某个更强音的整数倍泛音"压掉。
    """
    import librosa
    fmin = librosa.note_to_hz("A0")
    fmax_want = librosa.note_to_hz("C8")
    bins_per_octave = 12 * bps
    n_bins = int(np.ceil(np.log2(fmax_want / fmin) * bins_per_octave)) + 1
    # 保险：绝不能越过 Nyquist
    while fmin * 2 ** (n_bins / bins_per_octave) > sr / 2 and n_bins > bins_per_octave:
        n_bins -= 1

    cqt = np.abs(librosa.cqt(y, sr=sr, hop_length=hop, fmin=fmin,
                             n_bins=n_bins, bins_per_octave=bins_per_octave))
    times = librosa.times_like(cqt[0], sr=sr, hop_length=hop)
    # 每帧归一化，消除整体音量影响（力度后面单独估）
    peak = cqt.max(axis=0, keepdims=True)
    peak[peak <= 0] = 1.0
    norm = cqt / peak

    # 泛音抑制的比值门槛：泛音通常比基频弱，但钢琴上强弱悬殊，取 0.55
    HARM_RATIO = 0.55
    HARMONICS = (1, 2, 3)          # 八度 / 双八度 / 三八度

    pitches: list[list[int]] = []
    energies: list[list[float]] = []
    for fi in range(cqt.shape[1]):
        col = norm[:, fi]
        idx = [b for b in range(1, n_bins - 1)
               if col[b] >= col[b - 1] and col[b] >= col[b + 1] and col[b] > 0]
        idx.sort(key=lambda b: -col[b])

        chosen: dict[int, float] = {}          # 已选音 → 它的能量
        for b in idx:
            if len(chosen) >= max_poly:
                break
            note = PIANO_LOW + b // bps
            if not (PIANO_LOW <= note <= PIANO_HIGH) or note in chosen:
                continue
            e = float(col[b])
            if harmonic_suppress:
                # 只要它某个更低八度的音已被选中，且自己明显更弱，就当泛音丢掉
                is_harmonic = False
                for k in HARMONICS:
                    low = note - 12 * k
                    if low in chosen and e < HARM_RATIO * chosen[low]:
                        is_harmonic = True
                        break
                if is_harmonic:
                    continue
            chosen[note] = e

        order = sorted(chosen)
        pitches.append(order)
        energies.append([chosen[n] for n in order])
    return times, pitches, energies, cqt


def peaks_to_notes(times, pitches, energies, thresh: float = 0.10,
                   min_len: float = 0.06, max_gap: float = 0.05):
    """把逐帧音高聚成音符：同一音高连续出现的帧合并成一个 note。"""
    if len(times) < 2:
        return []
    hop = float(times[1] - times[0])
    active: dict[int, dict] = {}
    notes: list[dict] = []
    for fi, t in enumerate(times):
        here = {p: e for p, e in zip(pitches[fi], energies[fi]) if e >= thresh}
        for p in list(active):
            if p not in here:
                # 允许短暂中断（颤音/换音）—— 断太久才算结束
                if t - active[p]["last"] > max_gap + hop:
                    n = active.pop(p)
                    if n["last"] - n["start"] >= min_len:
                        notes.append(n)
        for p, e in here.items():
            if p in active:
                active[p]["last"] = t
                active[p]["energy"] = max(active[p]["energy"], e)
                active[p]["frames"] += 1
            else:
                active[p] = {"pitch": p, "start": t, "last": t,
                             "energy": e, "frames": 1}
    for p, n in active.items():
        if n["last"] - n["start"] >= min_len:
            notes.append(n)

    notes.sort(key=lambda n: (n["start"], n["pitch"]))
    for n in notes:
        n["end"] = n["last"] + hop
    return notes


def estimate_tempo_bpm(times, notes) -> float:
    """用音符起音间隔的中位数粗估速度。

    这只影响**记谱**（一个小节装几拍），不影响音频听感 —— 音频转出来时
    时间轴已经是绝对的，速度记号只是写给谱面看的。
    """
    if len(notes) < 4:
        return 120.0
    onsets = np.array(sorted({round(n["start"], 3) for n in notes}))
    if len(onsets) < 3:
        return 120.0
    ioi = np.diff(onsets)
    ioi = ioi[(ioi > 0.05) & (ioi < 2.0)]
    if len(ioi) == 0:
        return 120.0
    med = float(np.median(ioi))
    bpm = 60.0 / med if med > 0 else 120.0
    while bpm < 60:          # 把估计值折到合理区间
        bpm *= 2
    while bpm > 200:
        bpm /= 2
    return round(bpm, 1)


def transcribe(src: Path, dst: Path, thresh: float = 0.10, min_len: float = 0.06,
               max_poly: int = 6, hop: int = 512, bpm: float | None = None,
               verbose: bool = True) -> dict:
    """音频文件 → MIDI 文件。返回统计信息。"""
    import pretty_midi
    y, sr = load_audio(Path(src))
    dur = len(y) / sr
    times, pitches, energies, cqt = spectral_peaks(y, sr, hop=hop, max_poly=max_poly)
    notes = peaks_to_notes(times, pitches, energies, thresh=thresh, min_len=min_len)
    est_bpm = bpm or estimate_tempo_bpm(times, notes)

    pm = pretty_midi.PrettyMIDI(initial_tempo=est_bpm)
    ins = pretty_midi.Instrument(program=0, name="Transcribed")
    for n in notes:
        vel = int(np.clip(40 + 70 * n["energy"], 20, 120))
        ins.notes.append(pretty_midi.Note(velocity=vel, pitch=int(n["pitch"]),
                                          start=float(n["start"]),
                                          end=float(n["end"])))
    pm.instruments.append(ins)
    pm.write(str(dst))

    info = {
        "src": str(src), "dst": str(dst), "duration": round(dur, 2),
        "sr": sr, "n_notes": len(notes), "bpm": est_bpm,
        "note_density": round(len(notes) / max(dur, 1e-6), 2),
        "params": {"thresh": thresh, "min_len": min_len, "max_poly": max_poly},
    }
    if verbose:
        print(f"读入 {Path(src).name}  {dur:.1f}s  {sr} Hz")
        print(f"  估计速度 {est_bpm} BPM")
        print(f"  转录出 {len(notes)} 个音符（{info['note_density']} 音/秒）")
        print(f"  输出 {Path(dst).name}  {Path(dst).stat().st_size / 1024:.1f} KB")
        print()
        print("  ⚠️ 音频转 MIDI 本质有损：漏音、错音、多音、时值不准都是常态。")
        print("     缺 MIDI 时可以拿它当草稿；有 MIDI 就一定要用 MIDI。")
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="音频 → MIDI 转录（轻量频谱法，无深度学习依赖）")
    ap.add_argument("--src", required=True, help="输入音频：mp3 / wav / flac / ogg / m4a …")
    ap.add_argument("--out", default=None, help="输出 .mid（默认与输入同名）")
    ap.add_argument("--thresh", type=float, default=0.10,
                    help="峰值阈值 0~1，越小越灵敏（也越多垃圾音）")
    ap.add_argument("--min-len", type=float, default=0.06,
                    help="最短音符秒数，滤掉过短的毛刺")
    ap.add_argument("--max-poly", type=int, default=6,
                    help="每帧最多同时几个音（和弦密度上限）")
    ap.add_argument("--hop", type=int, default=512, help="帧移，越小时间分辨率越高")
    ap.add_argument("--bpm", type=float, default=None, help="覆盖速度估计")
    a = ap.parse_args(argv)

    src = Path(a.src).expanduser()
    if not src.exists():
        raise SystemExit(f"找不到输入：{src}")
    dst = Path(a.out).expanduser() if a.out else src.with_suffix(".mid")
    transcribe(src, dst, thresh=a.thresh, min_len=a.min_len,
               max_poly=a.max_poly, hop=a.hop, bpm=a.bpm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
