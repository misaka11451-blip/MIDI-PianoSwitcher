"""runner —— 把整条流水线串起来的编排层（六个阶段）。

main() 的 argparse 定义和阶段调度都放这里，core.py 只保留纯函数，
方便单独 import 做单元测试。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pretty_midi

from .core import (
    GM_PIANO, Log, _jclean, analyze, assign_hands, choose_split, find_musescore,
    fix_retrigger, hhmmss, make_piano, normalize_loudness, print_analysis,
    rebar_notes, render_audio, validate_musicxml, write_score_xml,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="MIDI → 纯钢琴版（音频 / MIDI / 钢琴谱），自带结构校验与自动修复")
    ap.add_argument("--src", required=True, help="输入 .mid / .midi")
    ap.add_argument("--outdir", default="mps_out")
    ap.add_argument("--name", default=None, help="输出文件名前缀（默认取输入文件名）")
    ap.add_argument("--piano", default="piano", choices=sorted(GM_PIANO),
                    help="钢琴音色，默认 acoustic grand piano")
    ap.add_argument("--mute-perc", action="store_true", default=True)
    ap.add_argument("--keep-perc", dest="mute_perc", action="store_false",
                    help="保留打击乐（会变成钢琴敲击音，一般不要）")
    ap.add_argument("--drop-dup", default="auto",
                    help="重复轨：auto / none / 轨号逗号分隔（如 2,3）")
    ap.add_argument("--retrigger-ms", type=float, default=25.0,
                    help="同音高之间至少留的间隙（毫秒），0 = 不修")
    ap.add_argument("--split", type=int, default=None,
                    help="左右手分界音高；不给就自动挑（让两手音符数最接近）")
    ap.add_argument("--no-score", dest="score", action="store_false", default=True,
                    help="不输出钢琴谱")
    ap.add_argument("--audio", action="store_true", default=True, help="渲染音频（默认开）")
    ap.add_argument("--no-audio", dest="audio", action="store_false")
    ap.add_argument("--audio-format", default="wav,mp3",
                    help="音频输出格式，逗号分隔。可选 wav / flac / mp3 / ogg。"
                         "例：--audio-format flac  或  --audio-format wav,flac,mp3")
    ap.add_argument("--bitdepth", type=int, default=16, choices=(16, 24),
                    help="无损格式（wav/flac）的位深，默认 16；24 = 更高动态余量")
    ap.add_argument("--mp3-bitrate", type=int, default=320,
                    help="MP3 码率 kbps，默认 320")
    ap.add_argument("--mp3", action="store_true", default=False,
                    help="已弃用：等价于 --audio-format 里包含 mp3")
    ap.add_argument("--no-mp3", dest="mp3", action="store_false")
    ap.add_argument("--pdf", action="store_true", help="钢琴谱同时导 PDF")
    ap.add_argument("--bpm", type=float, default=None, help="覆盖速度")
    ap.add_argument("--time", default=None, help="拍号，如 4/4；默认读 MIDI 里的")
    ap.add_argument("--lufs", type=float, default=-14.0, help="音频目标响度")
    ap.add_argument("--grid", type=float, default=0.25, help="量化网格（四分音符单位）")
    ap.add_argument("--title", default=None)
    ap.add_argument("--edit", default=None,
                    help="在转换前先做剪辑。可以是 ①预设 JSON 文件路径，"
                         "或 ②内联 JSON 数组，例如 "
                         "'[{\"kind\":\"trim\",\"start\":10,\"end\":20}]'。"
                         "支持 trim / duplicate / pitch / volume，按顺序施加。")
    ap.add_argument("--save-preset", default=None,
                    help="把 --edit 的操作清单另存为预设 JSON，方便以后复用")
    ap.add_argument("--export-edited-midi", action="store_true",
                    help="额外输出一份「剪辑后但还没转钢琴」的 MIDI，便于核对修改是否生效")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    # --audio-format 解析与校验
    fmts = [f.strip().lower() for f in a.audio_format.split(",") if f.strip()]
    allowed = {"wav", "flac", "mp3", "ogg"}
    bad = [f for f in fmts if f not in allowed]
    if bad:
        raise SystemExit(f"--audio-format 里有不支持的格式 {bad}；可选 {sorted(allowed)}")
    if a.mp3 and "mp3" not in fmts:
        fmts.append("mp3")
    a.audio_format = fmts or ["wav"]

    log = Log()
    src = Path(a.src).expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"找不到输入：{src}")
    outdir = Path(a.outdir).expanduser()
    if not outdir.is_absolute():
        outdir = Path.cwd() / outdir
    outdir.mkdir(parents=True, exist_ok=True)
    stem = a.name or src.stem
    program = GM_PIANO[a.piano]

    log.head(f"MIDI-PianoSwitcher —— {src.name} → 纯钢琴版")

    # ---------- 读 ----------
    pm = pretty_midi.PrettyMIDI(str(src))
    tempos = pm.get_tempo_changes()
    bpm = a.bpm or (float(tempos[1][0]) if len(tempos[1]) else 120.0)
    ts = pm.time_signature_changes[0] if pm.time_signature_changes else None
    ts_num, ts_den = (ts.numerator, ts.denominator) if ts else (4, 4)
    if a.time:
        num, den = a.time.split("/")
        ts_num, ts_den = int(num), int(den)
    log(f"  速度 {bpm:.1f} BPM   拍号 {ts_num}/{ts_den}")

    # ---------- 0 剪辑（DIY 操作，可选）----------
    # 放在最前面：用户按"原曲时间轴"给的时间点，必须在删重复轨/改音色之前生效，
    # 否则时间轴会被前面的步骤改掉，用户输入的秒数就对不上了。
    edited_midi_out = None
    if a.edit:
        log.head("【0/6】应用剪辑操作")
        from . import ops as ops_mod
        raw_duration = pm.get_end_time()
        # ① 先试着当文件路径读，② 再试着当内联 JSON
        preset = None
        p = Path(a.edit).expanduser()
        if p.exists() and p.is_file():
            preset = ops_mod.Preset.from_json(p)
            log(f"  从预设文件读入：{p.name}（{len(preset.operations)} 条操作）")
        else:
            try:
                payload = json.loads(a.edit)
            except Exception as e:
                raise SystemExit(f"--edit 既不是存在的文件，也不是合法 JSON：{e}")
            preset = (ops_mod.Preset.from_dict(payload) if isinstance(payload, dict)
                      else ops_mod.Preset(operations=[ops_mod.Operation.from_dict(o)
                                                      for o in payload]))
            log(f"  从内联 JSON 读入：{len(preset.operations)} 条操作")

        for w in ops_mod.validate_against_duration(preset.operations, raw_duration):
            log(f"  ⚠️ {w}")
        if a.save_preset:
            Path(a.save_preset).expanduser().write_text(
                preset.to_json(), encoding="utf-8")
            log(f"  预设已另存：{a.save_preset}")

        before = sum(len(i.notes) for i in pm.instruments)
        pm, edit_report = ops_mod.apply(pm, preset.operations)
        after = sum(len(i.notes) for i in pm.instruments)
        log(f"  原曲时长 {hhmmss(raw_duration)} → 剪辑后 {hhmmss(pm.get_end_time())}")
        log(f"  音符 {before} → {after}")
        for r in edit_report:
            stats = "  ".join(f"{k}={v}" for k, v in r.items()
                              if k not in ("index", "kind", "describe"))
            log(f"    {r['index'] + 1}. {r['describe']}"
                + (f"   [{stats}]" if stats else ""))
        if a.export_edited_midi:
            edited_midi_out = outdir / f"{stem}_edited.mid"
            pm.write(str(edited_midi_out))
            log(f"  ✓ 剪辑后 MIDI（未转钢琴）  {edited_midi_out.name}")

    # ---------- 1 诊断 ----------
    info = analyze(pm, 120.0)
    print_analysis(info, log)

    # ---------- 2 去重 + 改音色 + 静音鼓 ----------
    log.head("【2/6】去掉重复轨 + 统一钢琴音色 + 静音打击乐")
    if a.drop_dup == "auto":
        drop = {d["drop"] for d in info["duplicates"]}
    elif a.drop_dup == "none":
        drop = set()
    else:
        drop = {int(x) for x in re.split(r"[,\s]+", a.drop_dup) if x.strip()}
    stat = make_piano(pm, info, drop, program, a.mute_perc, 60.0 / bpm)
    log(f"  操作：{stat if stat else '（无需改动）'}")
    for i in sorted(drop):
        name = info["tracks"][i]["name"] if i < len(info["tracks"]) else str(i)
        log(f"    - 删除轨{i} 《{name}》")

    # ---------- 3 修重击 ----------
    log.head("【3/6】修同音高零间隔重击（消采样器咔哒声）")
    if a.retrigger_ms > 0:
        gap_sec = a.retrigger_ms / 1000.0
        adjusted, scanned = fix_retrigger(pm, gap_sec)
        log(f"  扫描 {scanned} 对相邻同音高音符，削短 {adjusted} 处音符尾部")
        log("  （只动 note 结束时刻，起音一个都没动 → 节奏不变）")
    else:
        log("  已跳过（--retrigger-ms 0）")

    # ---------- 写纯钢琴 MIDI ----------
    midi_out = outdir / f"{stem}_piano.mid"
    pm.write(str(midi_out))
    log(f"\n  ✓ MIDI  {midi_out.name}  {midi_out.stat().st_size / 1024:.1f} KB")

    # ---------- 4 分左右手 + 写谱 ----------
    score_out = pdf_out = None
    if a.score:
        log.head("【4/6】分左右手 + 生成钢琴谱")
        all_notes = [n for ins in pm.instruments for n in ins.notes]
        if not all_notes:
            log("  ⚠️ 没有任何音符，跳过乐谱")
        else:
            pitches = [n.pitch for n in all_notes]
            if a.split is not None:
                split = a.split
            else:
                split = int(np.median(pitches))
            rh, lh = assign_hands(all_notes, split)
            log(f"  分界音高 {split}（中位音高）→ 右手 {len(rh)} 音 / 左手 {len(lh)} 音")
            if not rh or not lh:
                # 全挤在一边：改用"让两手音符数最接近"的分界
                split2, ratio = choose_split(all_notes, int(min(pitches)), int(max(pitches)))
                rh, lh = assign_hands(all_notes, split2)
                log(f"  ⚠️ 原分界会把所有音符挤到一只手，自动改用 {split2}"
                    f"（右手占 {ratio * 100:.0f}%）→ 右 {len(rh)} / 左 {len(lh)}")
            if not rh or not lh:
                log("  ⚠️ 音域太窄，无法分两手，跳过乐谱")
            else:
                ev_rh = rebar_notes(rh, ts_num, ts_den, bpm, a.grid)
                ev_lh = rebar_notes(lh, ts_num, ts_den, bpm, a.grid)
                score_out = outdir / f"{stem}_piano.musicxml"
                try:
                    n_meas = write_score_xml([ev_rh, ev_lh], ts_num, ts_den, bpm,
                                             score_out, title=a.title or src.stem)
                    log(f"  ✓ 乐谱  {score_out.name}  {n_meas} 小节 "
                        f"{score_out.stat().st_size / 1024:.0f} KB")
                except Exception as e:
                    score_out = None
                    log(f"  ✗ 写谱失败：{type(e).__name__}: {e}")

    # ---------- 5 结构校验 ----------
    if score_out:
        log.head("【5/6】MusicXML 结构校验")
        v = validate_musicxml(score_out, ts_num, ts_den)
        log(f"  小节数 {v['n_measures']}   声部 {len(v['parts'])}")
        for p in v["parts"]:
            flag = "✓" if p["n_overflow"] == 0 else f"✗ {p['n_overflow']} 个撑爆"
            log(f"    part{p['index']}: {p['measures']} 小节  {flag}")
        if not v["ok"]:
            log("  ✗ 校验不通过：")
            for prob in v["problems"]:
                log(f"      · {prob}")
        else:
            log("  ✓ 全部小节时值合法，各声部小节数一致 → MuseScore 可收")

    # ---------- 6 渲染 ----------
    # raw_wav = MuseScore 的中间产物（永远要它）；
    # audio_outputs = 用户要的最终音频（wav/flac/mp3/ogg 任意组合）
    raw_wav = None
    audio_outputs: list[Path] = []
    if a.audio:
        log.head("【6/6】渲染音频")
        ms = find_musescore()
        if not ms:
            log("  ✗ 找不到 MuseScore，跳过音频（MIDI 和乐谱已生成）")
        else:
            log(f"  渲染器 {ms}")
            raw_wav = outdir / f".{stem}_render_tmp.wav"
            ok = render_audio(ms, midi_out, raw_wav, log, xml=score_out)
            if ok:
                import soundfile as sf
                data, sr = sf.read(str(raw_wav), dtype="float32", always_2d=True)
                dur = len(data) / sr
                log(f"  渲染完成 {hhmmss(dur)}  {sr} Hz  {data.shape[1]}ch")
                data, ninfo = normalize_loudness(data, sr, a.lufs)
                if "before_lufs" in ninfo:
                    log(f"  响度 {ninfo['before_lufs']} → {ninfo.get('after_lufs', '?')} LUFS"
                        f"   峰值 {ninfo['peak']}"
                        + (f"   限幅 {ninfo['limiter_gr']} dB" if "limiter_gr" in ninfo else ""))
                elif "error" in ninfo:
                    log(f"  响度测量失败：{ninfo['error']}")

                # ---- 音频导出：按 --audio-format 逐个写 ----
                # 无损（wav/flac）走 libsndfile；有损（mp3/ogg）走 ffmpeg。
                #
                # ⚠️ OGG 绝对不能交给 libsndfile：实测它在这个环境里写 OGG 会
                #    原生崩溃（0xC0000409 stack buffer overrun），异常捕获不住，
                #    整个进程会静默消失、连汇总都打不出来。所以 OGG 只走 ffmpeg。
                spf = {"wav": "PCM_16", "flac": "PCM_16"}
                if a.bitdepth == 24:
                    spf["wav"] = "PCM_24"
                    spf["flac"] = "PCM_24"
                ff = shutil.which("ffmpeg")
                for fmt in a.audio_format:
                    dst = outdir / f"{stem}_piano.{fmt}"
                    okw = False
                    if fmt == "mp3":
                        if ff:
                            r = subprocess.run(
                                [ff, "-y", "-i", str(raw_wav), "-codec:a", "libmp3lame",
                                 "-b:a", f"{a.mp3_bitrate}k", str(dst)],
                                capture_output=True)
                            okw = r.returncode == 0 and dst.exists()
                        if not okw:
                            try:
                                sf.write(str(dst), data, sr, bitrate=f"{a.mp3_bitrate}k")
                                okw = dst.exists()
                            except Exception as e:
                                log(f"  ✗ {fmt.upper()} 导出失败：{type(e).__name__}: {e}")
                    elif fmt == "ogg":
                        if not ff:
                            log("  ✗ OGG 需要 ffmpeg（libsndfile 写 OGG 会崩溃），已跳过")
                        else:
                            r = subprocess.run(
                                [ff, "-y", "-i", str(raw_wav), "-codec:a", "libvorbis",
                                 "-q:a", "6", str(dst)],
                                capture_output=True)
                            okw = r.returncode == 0 and dst.exists()
                            if not okw:
                                log(f"  ✗ OGG 导出失败（ffmpeg 返回码 {r.returncode}）")
                    else:
                        try:
                            sf.write(str(dst), data, sr,
                                     format=fmt.upper(), subtype=spf[fmt])
                            okw = dst.exists() and dst.stat().st_size > 1024
                        except Exception as e:
                            log(f"  ✗ {fmt.upper()} 导出失败：{type(e).__name__}: {e}")
                    if okw:
                        extra = ""
                        if fmt == "flac":
                            extra = f"  无损 {a.bitdepth}bit"
                        elif fmt == "mp3":
                            extra = f"  {a.mp3_bitrate} kbps"
                        elif fmt == "wav":
                            extra = f"  PCM_{a.bitdepth}"
                        elif fmt == "ogg":
                            extra = "  Vorbis q6"
                        log(f"  ✓ {dst.name}  {dst.stat().st_size / 1024 / 1024:.1f} MB{extra}")
                        audio_outputs.append(dst)
            else:
                log("  ✗ 音频渲染失败（MIDI 和乐谱已生成，可用 MuseScore 手动导出）")

    # ---------- PDF ----------
    if a.pdf and score_out:
        ms = find_musescore()
        if ms:
            pdf_out = outdir / f"{stem}_piano.pdf"
            r = subprocess.run([str(ms), "-o", str(pdf_out), str(score_out)],
                               capture_output=True)
            if pdf_out.exists():
                log(f"  ✓ PDF   {pdf_out.name}  {pdf_out.stat().st_size / 1024:.0f} KB")
            else:
                pdf_out = None
                log(f"  ✗ PDF 导出失败（返回码 {r.returncode}）")

    # ---------- 汇总 ----------
    # 中间渲染文件不属于交付物，删掉
    if raw_wav and raw_wav.exists():
        try:
            raw_wav.unlink()
        except Exception:
            pass

    log.head("完成")
    made = []
    for p in [midi_out, score_out, pdf_out, edited_midi_out] + audio_outputs:
        if p and Path(p).exists():
            made.append((Path(p).name, Path(p).stat().st_size))
    for name, size in made:
        unit = f"{size / 1024 / 1024:.1f} MB" if size > 1024 * 1024 else f"{size / 1024:.0f} KB"
        log(f"  {name:<40} {unit}")
    log(f"\n输出目录：{outdir}")

    if not a.quiet:
        (outdir / f"{stem}_report.json").write_text(
            json.dumps(_jclean({
                "source": str(src), "bpm": bpm, "time_signature": f"{ts_num}/{ts_den}",
                "analysis": info, "piano_stats": stat,
                "outputs": [str(p) for p in [midi_out, score_out, pdf_out] + audio_outputs
                            if p],
            }), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())


