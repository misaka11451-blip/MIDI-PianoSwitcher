#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端测试 MIDI-PianoSwitcher GUI 的 HTTP 接口。

用标准库 urllib 手写 multipart（顺便验证服务端解析器能处理真实浏览器风格的请求）。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
MIDI = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("stress.mid")
OUTDIR = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(".")
OUTDIR.mkdir(parents=True, exist_ok=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"    {'✓' if cond else '✗'} {name}" + (f"  —— {detail}" if detail else ""))


def post_json(path: str, payload: dict, timeout: int = 900):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def post_file(path: str, field: str, filename: str, content: bytes, timeout: int = 900):
    boundary = "----MIDI-PianoSwitcherTest" + uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="{field}"; '
             f'filename="{filename}"\r\n').encode("utf-8")
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += content
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def get_raw(path: str, timeout: int = 300):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def main() -> int:
    print("=" * 72)
    print("  MIDI-PianoSwitcher GUI 接口端到端测试")
    print("=" * 72)
    print(f"  服务: {BASE}")
    print(f"  素材: {MIDI}  ({MIDI.stat().st_size / 1024:.1f} KB)")
    print()

    # ---------- 0 健康检查 ----------
    print("【0】健康检查 /api/health")
    st, body, _ = get_raw("/api/health")
    check("HTTP 200", st == 200, f"got {st}")
    check("musescore 可用", bool(body) and json.loads(body).get("musescore"),
          str(json.loads(body) if body else {}))
    print()

    # ---------- 1 上传 ----------
    print("【1】上传 MIDI /api/project")
    # 故意用中文文件名，验证 multipart 解析器能保住原始字节
    cn_name = "测试曲目.mid"
    st, proj = post_file("/api/project", "file", cn_name, MIDI.read_bytes())
    check("HTTP 200", st == 200, str(proj.get("error", "")))
    if st != 200:
        return 1
    print(f"      文件: {proj['filename']}   时长 {proj['duration']}s   "
          f"{proj['n_tracks']} 轨 / {proj['n_notes']} 音符   {proj['bpm']} BPM "
          f"{proj['time_signature']}")
    check("中文文件名被正确解码", proj["filename"] == cn_name, proj["filename"])
    check("时长 > 0", proj["duration"] > 0)
    check("音符数 > 0", proj["n_notes"] > 0)
    check("波形峰值点数 = 900", len(proj["peaks"]) == 900, str(len(proj["peaks"])))
    check("峰值都在 0~1", all(0.0 <= p <= 1.0 for p in proj["peaks"]))
    check("峰值不是全 0", max(proj["peaks"]) > 0.1, f"max={max(proj['peaks']):.3f}")
    check("检测到重复轨", len(proj["duplicate_tracks"]) > 0,
          str(proj["duplicate_tracks"]))
    check("有提示警告", len(proj["warnings"]) > 0)
    for w in proj["warnings"]:
        print(f"      ⚠ {w}")
    pid = proj["project_id"]
    print()

    # ---------- 2 试听 + 剪辑 ----------
    print("【2】试听 /api/preview （4 条剪辑操作）")
    operations = [
        {"kind": "trim", "start": 4.0, "end": 8.0},
        {"kind": "duplicate", "start": 0.0, "end": 2.0, "repeat": 1},
        {"kind": "pitch", "start": 12.0, "end": 16.0, "semitones": 12},
        {"kind": "volume", "start": 0.0, "end": 6.0, "db": -6},
    ]
    st, prev = post_json("/api/preview", {"project_id": pid, "operations": operations})
    check("HTTP 200", st == 200, str(prev.get("error", "")))
    if st != 200:
        return 1
    print(f"      剪辑后时长 {prev['duration']}s   试听文件 {prev['size'] / 1024:.0f} KB")
    check("返回音频地址", bool(prev.get("audio_url")), str(prev.get("audio_url")))
    check("时长变了（删4秒+复制2秒 → 原24秒应变22秒）",
          abs(prev["duration"] - 22.0) < 1.5, f"{prev['duration']}")
    check("执行报告 4 条", len(prev["report"]) == 4, str(len(prev["report"])))
    for r in prev["report"]:
        extra = "  ".join(f"{k}={v}" for k, v in r.items()
                          if k not in ("index", "kind", "describe"))
        print(f"      {r['index'] + 1}. {r['describe']}   [{extra}]")
    kinds = [r["kind"] for r in prev["report"]]
    check("报告顺序与输入一致", kinds == ["trim", "duplicate", "pitch", "volume"], str(kinds))
    trim_r = prev["report"][0]
    check("trim 删掉了音符", trim_r.get("删除音符", 0) > 0, str(trim_r))
    pitch_r = prev["report"][2]
    check("pitch 移调了音符", pitch_r.get("移调音符", 0) > 0, str(pitch_r))
    vol_r = prev["report"][3]
    check("volume 改了力度", vol_r.get("改力度音符", 0) > 0, str(vol_r))
    print()

    # ---------- 3 下载试听音频 ----------
    print("【3】下载试听音频")
    st, raw, ctype = get_raw(prev["audio_url"])
    check("HTTP 200", st == 200, f"got {st}")
    check("有内容", len(raw) > 5000, f"{len(raw)} 字节")
    is_mp3 = raw[:3] == b"ID3" or raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
    check("是有效 MP3（ID3 或帧同步头）", is_mp3, repr(raw[:4]))
    dst = OUTDIR / "preview_downloaded.mp3"
    dst.write_bytes(raw)
    print(f"      已保存 {dst}  ({len(raw) / 1024:.0f} KB)")
    print()

    # ---------- 4 完整渲染 ----------
    print("【4】完整渲染 /api/render")
    st, ren = post_json("/api/render", {
        "project_id": pid, "operations": operations,
        "audio_format": ["mp3", "flac"], "bitdepth": 24,
        "want_score": True, "want_pdf": True, "lufs": -14.0,
        "piano": "piano", "drop_dup": "auto",
    })
    check("HTTP 200", st == 200, str(ren.get("error", "")))
    if st != 200:
        return 1
    names = [f["name"] for f in ren["files"]]
    print(f"      产物: {names}")
    print(f"      乐谱: {ren.get('score_url')}")
    check("有 MP3 产物", any(n.endswith(".mp3") for n in names), str(names))
    check("有 FLAC 产物", any(n.endswith(".flac") for n in names), str(names))
    check("有 MIDI 产物", any(n.endswith(".mid") for n in names), str(names))
    check("有乐谱产物", bool(ren.get("score_url")), str(ren.get("score_url")))
    check("每个产物都有 URL 和大小",
          all(f.get("url") and f.get("size", 0) > 0 for f in ren["files"]))
    print()

    # ---------- 5 下载产物 ----------
    print("【5】下载产物")
    for f in ren["files"]:
        st, raw, ctype = get_raw(f["url"])
        ok = st == 200 and len(raw) == f["size"]
        check(f"下载 {f['name']}", ok, f"HTTP {st}, {len(raw)} 字节")
    if ren.get("score_url"):
        st, raw, _ = get_raw(ren["score_url"])
        check("乐谱是有效 PDF（%PDF 签名）", st == 200 and raw[:4] == b"%PDF",
              repr(raw[:4]))
        (OUTDIR / "score_downloaded.pdf").write_bytes(raw)
        print(f"      已保存 {OUTDIR / 'score_downloaded.pdf'}")
    print()

    # ---------- 6 错误处理 ----------
    print("【6】错误处理")
    st, r = post_json("/api/preview", {"project_id": "0" * 32, "operations": []})
    check("假项目 ID → 400", st == 400, f"HTTP {st}: {r.get('error')}")

    st, r = post_json("/api/preview",
                      {"project_id": pid, "operations": [{"kind": "explode", "start": 1, "end": 2}]})
    check("非法操作类型 → 400", st == 400, f"HTTP {st}: {r.get('error')}")

    st, r = post_json("/api/preview",
                      {"project_id": pid, "operations": [{"kind": "trim", "start": 9, "end": 1}]})
    check("end < start → 400", st == 400, f"HTTP {st}: {r.get('error')}")

    st, r = post_json("/api/preview", {"project_id": "../../etc", "operations": []})
    check("路径穿越式项目 ID → 400", st == 400, f"HTTP {st}: {r.get('error')}")

    st, raw, _ = get_raw(f"/api/files/{pid}/..%2f..%2fproject.json")
    check("文件名路径穿越被拒", st in (403, 400, 404), f"HTTP {st}")

    st, r = post_file("/api/project", "file", "evil.txt", b"this is not midi")
    check("上传非 MIDI → 400", st == 400, f"HTTP {st}: {r.get('error')}")

    st, r = post_file("/api/project", "file", "broken.mid", b"MThd\x00\x00\x00\x06garbage")
    check("上传损坏 MIDI → 400", st == 400, f"HTTP {st}: {r.get('error')}")

    st, r = post_json("/api/nonexistent", {})
    check("未知接口 → 404", st == 404, f"HTTP {st}")

    print()
    print("=" * 72)
    print(f"  通过 {len(PASS)}   失败 {len(FAIL)}")
    if FAIL:
        print("  失败项：")
        for f in FAIL:
            print(f"    ✗ {f}")
    print("=" * 72)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
