"""webapp —— MIDI-PianoSwitcher 的图形界面（本地 Web UI）。

## 为什么选 Web UI 而不是 Qt/Tk

* **零新增重依赖**。界面用 Python 内置的 ``http.server`` 发出，渲染交给浏览器。
  对比：PySide6/PyQt 是 100+ MB 的依赖，塞进一个"轻量工具"并不合适。
* **真正的桌面应用体验**。``midi-pianoswitcher-gui`` 一条命令启动、自动开浏览器，
  关掉终端就结束，不常驻后台、不联网、不上传任何文件到外部。
* **浏览器是现成的渲染引擎**：波形、拖拽选区、音频播放（HTML5 audio）
  都不用自己写。

## 架构

    ┌──────────── 浏览器（static/ 里的 index.html + app.css + app.js）───────────┐
    │  上传 MIDI · 画波形 · 拖拽框选区间 · 编辑操作列表 · 播放试听 · 下载产物      │
    └───────────────────────────────┬───────────────────────────────────────────┘
                                    │  JSON over HTTP（仅 127.0.0.1）
    ┌───────────────────────────────┴───────────────────────────────────────────┐
    │  webapp.py（本文件）                                                        │
    │    /api/project  上传 → 解剖 + 波形峰值 + 项目临时目录                       │
    │    /api/preview  ops 剪辑 → 渲染 mp3（快，用于试听）                          │
    │    /api/render   ops 剪辑 → 完整产物（音频/PDF/乐谱）                         │
    │    /api/files/…  下载产物                                                    │
    └───────────────────────────────────────────────────────────────────────────┘

## 安全

只监听 127.0.0.1（不对外暴露）；项目 ID 是随机 token，文件名经过白名单校验，
不存在路径穿越。仍然**不要**把这个端口转发到公网 —— 它设计成单机自用。
"""
from __future__ import annotations

import io
import json
import mimetypes
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pretty_midi

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from . import ops as ops_mod
from .core import _jclean, find_musescore, hhmmss, render_audio
from .runner import main as run_pipeline


# --------------------------------------------------------------------------- #
# multipart/form-data 解析
# --------------------------------------------------------------------------- #
def _decode_param(raw: bytes | None) -> str | None:
    """把 header 里的原始字节还原成 Unicode 文件名/字段名。

    浏览器上传 ``测试.mid`` 时，header 里放的是**原始 UTF-8 字节**。
    所以这里必须拿到**字节**再按 UTF-8 解码 —— 一旦先被某个库按
    ASCII/latin-1 解成 str，就可能已经变成 U+FFFD 替换字符，
    信息永久丢失、再怎么转都救不回来（Python 3.14 的 email 模块就是这样，
    实测 ``get_filename()`` 返回 '������.mid'）。
    """
    if raw is None:
        return None
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _parse_disposition(header: bytes) -> tuple[str | None, str | None]:
    """从 Content-Disposition 的**原始字节**里取出 (name, filename)。

    自己解析而不是用 email 模块，就是为了保住文件名里的原始字节。
    同时兼容 RFC 5987 的 ``filename*=UTF-8''%E6%B5%8B%E8%AF%95.mid`` 写法。
    """
    name = fname = None
    for m in re.finditer(rb';\s*([A-Za-z0-9_*\-]+)\s*=\s*("([^"]*)"|([^;\r\n]+))',
                         header):
        key = m.group(1).lower()
        val = m.group(3) if m.group(3) is not None else m.group(4)
        if val is None:
            continue
        val = val.strip()
        if key == b"name":
            name = _decode_param(val)
        elif key == b"filename":
            fname = _decode_param(val)
        elif key == b"filename*":
            # 形如 UTF-8''%E6%B5%8B%E8%AF%95.mid
            s = val.decode("latin-1", "replace")
            parts = s.split("''", 1)
            encoded = parts[1] if len(parts) == 2 else s
            fname = urllib.parse.unquote(encoded)
    return name, fname


def parse_multipart(body: bytes, content_type: str) -> dict[str, dict]:
    """把 multipart/form-data 解析成 ``{字段名: {"filename":..., "data": bytes}}``。

    **全程手写字节解析**，两个理由：

    1. ``cgi.FieldStorage`` 从 Python 3.13 起已从标准库移除，直接 ImportError；
       又不想为一个本地小工具拖进 ``multipart``/``werkzeug`` 这种依赖。
    2. 用 ``email`` 模块虽然能切分，但它在解析 header 时会**替换掉非 ASCII
       字节**，导致中文文件名变成一串 U+FFFD（实测）。

    这里只处理 multipart 里最常用的两种 part：普通字段和带文件名的字段 ——
    对本工具的上传场景完全够用。
    """
    m = re.search(r'boundary\s*=\s*"([^"]+)"', content_type) or \
        re.search(r"boundary\s*=\s*([^;\s]+)", content_type)
    if not m:
        raise ValueError("multipart 请求缺少 boundary")
    boundary = m.group(1).strip().strip('"').encode("latin-1")
    delim = b"--" + boundary

    out: dict[str, dict] = {}
    # 先按分隔符切开；第一段是开头的 preamble，最后一段是结束标记
    chunks = body.split(delim)
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):        # 结束标记
            break
        chunk = chunk.lstrip(b"\r\n")
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        # 去掉尾部属于下一个分隔符的换行
        if data.endswith(b"\r\n"):
            data = data[:-2]

        name = fname = None
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-disposition:"):
                name, fname = _parse_disposition(line)
        if name:
            out[name] = {"filename": fname, "data": data}
    return out


STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
MIDI_EXT = (".mid", ".midi", ".kar", ".rmi")
PROJECT_RE = re.compile(r"^[a-f0-9]{16,}$")

# 每个项目的渲染锁：MuseScore 一次吃几百 MB，串行更稳
_render_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(pid: str) -> threading.Lock:
    with _locks_guard:
        return _render_locks.setdefault(pid, threading.Lock())


# --------------------------------------------------------------------------- #
# 波形峰值
# --------------------------------------------------------------------------- #
def compute_peaks(pm: pretty_midi.PrettyMIDI, n: int = 900) -> list[float]:
    """把合成音频压成 n 个 0–1 的峰值，给前端画波形。

    用 ``pretty_midi.synthesize`` 而不是自己写采样器：它借 fluidsynth 的
    正弦波合成（FluidSynth 不可用时退化成简单正弦），够画一个"像样"的
    能量包络，且不用等 MuseScore 慢渲染。
    """
    try:
        audio = pm.synthesize(fs=8000)
    except Exception:
        return [0.0] * n
    if audio.size == 0:
        return [0.0] * n
    mono = np.abs(audio).astype(np.float32)
    # 分块取块内峰值
    idx = np.linspace(0, mono.size, n + 1).astype(int)
    peaks = [float(mono[a:b].max()) if b > a else 0.0
             for a, b in zip(idx[:-1], idx[1:])]
    m = max(peaks) if peaks else 0.0
    if m > 0:
        peaks = [min(1.0, p / m) for p in peaks]
    return peaks


def describe_tracks(pm: pretty_midi.PrettyMIDI) -> dict:
    """给 UI 展示的文件概要。"""
    from .core import analyze
    info = analyze(pm, 120.0)
    return {
        "n_tracks": len(pm.instruments),
        "n_notes": sum(len(i.notes) for i in pm.instruments),
        "duration": round(pm.get_end_time(), 3),
        "bpm": round(float(pm.get_tempo_changes()[1][0]), 2)
        if len(pm.get_tempo_changes()[1]) else 120.0,
        "time_signature": (
            f"{pm.time_signature_changes[0].numerator}/"
            f"{pm.time_signature_changes[0].denominator}"
            if pm.time_signature_changes else "4/4"),
        "duplicate_tracks": [
            {"a": d["a"], "b": d["b"], "similarity": round(d["sim"], 3),
             "keep": d["keep"], "drop": d["drop"]}
            for d in info["duplicates"]
        ],
        "drum_tracks": info["drums"],
        "tracks": [
            {"index": t["index"], "name": t["name"], "notes": t["notes"],
             "program": t["program"], "is_drum": t["is_drum"],
             "range": t["range"]}
            for t in info["tracks"]
        ],
    }


# --------------------------------------------------------------------------- #
# 处理请求
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "midi-pianoswitcher-gui"
    projects_root: Path

    # ---- 基础设施 ----
    def log_message(self, fmt, *args):
        # 默认的日志太吵；只留有用的
        if "/api/" in (fmt % args):
            sys.stderr.write(f"  [{self.address_string()}] {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict[str, str] | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, code: int, payload: dict):
        # 统一走 _jclean：numpy 的 int64/float32 不是 JSON 可序列化类型，
        # 而 pretty_midi / librosa 到处返回 numpy 标量。在这里兜一次，
        # 防止将来任何新加的字段又把响应搞崩。
        try:
            text = json.dumps(_jclean(payload), ensure_ascii=False)
        except TypeError as e:
            text = json.dumps({"ok": False,
                               "error": f"响应序列化失败：{e}"}, ensure_ascii=False)
            code = 500
        self._send(code, text.encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, code: int, msg: str):
        self._json(code, {"ok": False, "error": msg})

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 4 * 1024 * 1024:
            raise ValueError("请求体过大（上限 4 MB）")
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"请求不是合法 JSON：{e}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是一个 JSON 对象")
        return data

    def _project_dir(self, pid: str) -> Path:
        if not PROJECT_RE.match(pid or ""):
            raise ValueError("项目 ID 不合法")
        d = self.projects_root / pid
        if not d.is_dir():
            raise ValueError("项目不存在或已过期，请重新上传文件")
        return d

    # ---- 路由 ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path == "/api/health":
                return self._json(200, {"ok": True, "musescore": bool(find_musescore()),
                                        "ffmpeg": bool(shutil.which("ffmpeg"))})
            if path.startswith("/api/files/"):
                try:
                    return self._serve_file(path)
                except ValueError as e:
                    # 非法/不存在的项目 ID 是**客户端输入问题**，必须回 400。
                    # 漏了这层捕获就会变成 500，前端也拿不到可读的提示。
                    return self._error(400, str(e))
            # 其它路径当静态资源
            rel = path.lstrip("/")
            if rel and (STATIC_DIR / rel).is_file():
                return self._serve_static(rel)
            return self._error(404, f"找不到 {path}")
        except Exception as e:
            traceback.print_exc()
            return self._error(500, f"服务器错误：{type(e).__name__}: {e}")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/project":
                return self._api_project()
            if path == "/api/preview":
                return self._api_preview()
            if path == "/api/render":
                return self._api_render()
            return self._error(404, f"找不到接口 {path}")
        except ValueError as e:
            return self._error(400, str(e))
        except Exception as e:
            traceback.print_exc()
            return self._error(500, f"处理失败：{type(e).__name__}: {e}")

    # ---- 静态文件 ----
    def _serve_static(self, rel: str):
        p = STATIC_DIR / rel
        # 防路径穿越
        try:
            p.resolve().relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self._error(403, "禁止访问该路径")
        if not p.is_file():
            return self._error(404, f"静态文件不存在：{rel}")
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, p.read_bytes(), ctype)

    # ---- 下载产物 ----
    def _serve_file(self, path: str):
        """``/api/files/{项目ID}[/{子目录}]/{文件名}``

        产物分放在 ``preview/``（试听）和 ``out/``（完整渲染）两个子目录里，
        所以这里要支持一层子目录。子目录名走**白名单**，并且最终路径必须
        仍是项目目录的后代 —— 两道一起才挡得住路径穿越。
        """
        rest = path[len("/api/files/"):]
        parts = [urllib.parse.unquote(x) for x in rest.split("/") if x]
        if len(parts) == 2:
            pid, sub, fname = parts[0], None, parts[1]
        elif len(parts) == 3:
            pid, sub, fname = parts
        else:
            return self._error(400, "路径格式应为 /api/files/{项目ID}[/{子目录}]/{文件名}")

        d = self._project_dir(pid)
        if sub is not None:
            if sub not in ("preview", "out"):
                return self._error(403, f"不允许访问该子目录：{sub}")
            d = d / sub
        # 文件名不许含分隔符、不许是隐藏文件
        if not fname or "/" in fname or "\\" in fname or fname.startswith("."):
            return self._error(403, "文件名不合法")

        f = (d / fname).resolve()
        # 双保险：解析后必须仍在项目根目录内（挡住 ../ 和符号链接）
        try:
            f.relative_to(self._project_dir(pid).resolve())
        except ValueError:
            return self._error(403, "禁止访问项目目录以外的文件")
        if not f.is_file():
            return self._error(404, f"产物不存在：{fname}")

        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype, extra={
            "Content-Disposition":
                f"inline; filename*=UTF-8''{urllib.parse.quote(fname)}"
        })

    # ---- /api/project ----
    def _api_project(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._error(400, "请用 multipart/form-data 上传文件")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._error(400, "请求体为空")
        if n > 32 * 1024 * 1024:
            return self._error(400, "MIDI 文件过大（上限 32 MB）")
        try:
            parts = parse_multipart(self.rfile.read(n), ctype)
        except ValueError as e:
            return self._error(400, f"上传数据解析失败：{e}")

        if "file" not in parts:
            return self._error(400, "表单里缺少 file 字段")
        raw = parts["file"]["data"]
        if not raw:
            return self._error(400, "上传的文件是空的")

        name = Path(parts["file"]["filename"] or "upload.mid").name
        suffix = Path(name).suffix.lower()
        if suffix not in MIDI_EXT:
            return self._error(
                400, f"只支持 MIDI 文件（{', '.join(MIDI_EXT)}）。"
                     f"音频转 MIDI 请先用命令行 midi_pianoswitcher.audio2midi，"
                     f"准确度有限，见 README。")

        pid = secrets.token_hex(16)
        d = self.projects_root / pid
        d.mkdir(parents=True, exist_ok=True)
        src = d / f"source{suffix}"
        src.write_bytes(raw)

        try:
            pm = pretty_midi.PrettyMIDI(str(src))
        except Exception as e:
            shutil.rmtree(d, ignore_errors=True)
            return self._error(400, f"这个文件读不出音符，可能不是有效 MIDI："
                                    f"{type(e).__name__}: {e}")

        summary = describe_tracks(pm)
        peaks = compute_peaks(pm)
        (d / "project.json").write_text(json.dumps(
            _jclean({**summary, "filename": name, "suffix": suffix}),
            ensure_ascii=False, indent=2), encoding="utf-8")

        return self._json(200, {
            "ok": True,
            "project_id": pid,
            "filename": name,
            "peaks": peaks,
            "warnings": self._project_warnings(summary),
            **summary,
        })

    @staticmethod
    def _project_warnings(s: dict) -> list[str]:
        w = []
        if s["duplicate_tracks"]:
            pairs = "、".join(f"轨{d['a']}↔轨{d['b']}(相似度{d['similarity']})"
                             for d in s["duplicate_tracks"])
            w.append(f"检测到重复声部：{pairs}。转钢琴时会自动删掉多余的那些，"
                     f"否则会变响发糊。")
        if s["drum_tracks"]:
            w.append(f"有 {len(s['drum_tracks'])} 条打击乐轨，转钢琴时会自动静音。")
        if s["duration"] > 600:
            w.append(f"曲子较长（{hhmmss(s['duration'])}），完整渲染可能要一两分钟。")
        if not s["n_notes"]:
            w.append("这个文件里没有音符。")
        return w

    # ---- 公共：解析操作 ----
    def _parse_ops(self, payload: dict) -> list[ops_mod.Operation]:
        raw = payload.get("operations") or []
        if not isinstance(raw, list):
            raise ValueError("operations 必须是数组")
        out = []
        for i, o in enumerate(raw):
            if not isinstance(o, dict):
                raise ValueError(f"第 {i + 1} 条操作不是对象")
            try:
                out.append(ops_mod.Operation.from_dict(o))
            except (ValueError, TypeError) as e:
                raise ValueError(f"第 {i + 1} 条操作有问题：{e}")
        return out

    # ---- /api/preview ----
    def _api_preview(self):
        payload = self._read_json()
        pid = str(payload.get("project_id") or "")
        d = self._project_dir(pid)
        operations = self._parse_ops(payload)

        src = next((d / f"source{e}" for e in MIDI_EXT
                    if (d / f"source{e}").exists()), None)
        if src is None:
            return self._error(400, "项目源文件缺失，请重新上传")

        pm = pretty_midi.PrettyMIDI(str(src))
        duration_in = pm.get_end_time()
        warnings = ops_mod.validate_against_duration(operations, duration_in)
        # 时间轴映射：波形用的是原始时间轴，试听音频是处理后的时间轴。
        # 把这个对应关系交给前端，播放头才能精确对齐（否则只能按比例硬凑）。
        segments = ops_mod.timeline_map(operations, duration_in)

        with _lock_for(pid):
            edited = d / "preview_edited.mid"
            _, report = ops_mod.apply(pm, operations)
            pm.write(str(edited))
            duration_out = pm.get_end_time()

            outdir = d / "preview"
            outdir.mkdir(exist_ok=True)
            # 复用主流水线：它会把非钢琴音色统一、修重击、然后渲染
            argv = ["--src", str(edited), "--outdir", str(outdir),
                    "--name", "preview", "--audio-format", "mp3",
                    "--mp3-bitrate", "192", "--no-score", "--quiet"]
            run_pipeline(argv)
            mp3 = outdir / "preview_piano.mp3"
            if not mp3.exists():
                return self._error(500, "试听渲染失败，请检查 MuseScore 是否可用")

        return self._json(200, {
            "ok": True,
            "audio_url": f"/api/files/{pid}/preview/{mp3.name}",
            "duration": round(duration_out, 3),
            "duration_in": round(duration_in, 3),
            "segments": segments,
            "report": report,
            "warnings": warnings,
            "size": mp3.stat().st_size,
        })

    # ---- /api/render ----
    def _api_render(self):
        payload = self._read_json()
        pid = str(payload.get("project_id") or "")
        d = self._project_dir(pid)
        operations = self._parse_ops(payload)

        fmts = payload.get("audio_format") or ["mp3"]
        allowed = {"wav", "flac", "mp3", "ogg"}
        fmts = [f for f in fmts if f in allowed] or ["mp3"]
        bitdepth = 24 if int(payload.get("bitdepth") or 16) == 24 else 16
        want_score = bool(payload.get("want_score", True))
        want_pdf = bool(payload.get("want_pdf", False))
        lufs = float(payload.get("lufs", -14.0))
        piano = str(payload.get("piano") or "piano")
        drop_dup = str(payload.get("drop_dup") or "auto")
        split = payload.get("split")

        src = next((d / f"source{e}" for e in MIDI_EXT
                    if (d / f"source{e}").exists()), None)
        if src is None:
            return self._error(400, "项目源文件缺失，请重新上传")

        pm = pretty_midi.PrettyMIDI(str(src))
        duration_in = pm.get_end_time()
        warnings = ops_mod.validate_against_duration(operations, duration_in)
        # 与 preview 一致：把时间轴映射也给出去，前端播放头才能精确对齐
        segments = ops_mod.timeline_map(operations, duration_in)

        with _lock_for(pid):
            edited = d / "edited.mid"
            _, report = ops_mod.apply(pm, operations)
            pm.write(str(edited))

            outdir = d / "out"
            outdir.mkdir(exist_ok=True)
            # 清掉上一轮的产物，免得新旧文件混在结果列表里
            for old in outdir.iterdir():
                if old.is_file():
                    try:
                        old.unlink()
                    except OSError:
                        pass

            argv = ["--src", str(edited), "--outdir", str(outdir),
                    "--name", "result",
                    "--audio-format", ",".join(fmts),
                    "--bitdepth", str(bitdepth),
                    "--lufs", str(lufs),
                    "--piano", piano,
                    "--drop-dup", drop_dup,
                    "--quiet"]
            if split:
                argv += ["--split", str(int(split))]
            if not want_score:
                argv.append("--no-score")
            if want_pdf:
                argv.append("--pdf")

            # 捕获流水线的输出当作「执行日志」回给前端。
            # 渲染要几十秒，这段时间用户只能盯着转圈；有了日志他至少能看到
            # 工具在做什么、做到哪一步了。
            buf = io.StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = buf
                run_pipeline(argv)
            finally:
                sys.stdout = old_stdout
            log_text = buf.getvalue()
            if len(log_text) > 200_000:
                log_text = log_text[:100_000] + "\n…（日志过长已截断）…\n" + log_text[-100_000:]

            files = []
            for f in sorted(outdir.iterdir()):
                if f.name.startswith(".") or not f.is_file():
                    continue
                if f.suffix.lower() in (".musicxml", ".pdf"):
                    continue          # 乐谱单独在下面给
                files.append({
                    "name": f.name,
                    "url": f"/api/files/{pid}/out/{f.name}",
                    "size": f.stat().st_size,
                })
            score = outdir / "result_piano.pdf"
            if not score.exists():
                mxl = outdir / "result_piano.musicxml"
                score = mxl if mxl.exists() else None
            duration_out = pm.get_end_time()

        final = {}
        for cand in ("result_piano.wav", "result_piano.flac", "result_piano.mp3",
                     "result_piano.ogg"):
            p = outdir / cand
            if p.exists():
                final["duration"] = round(duration_out, 3)

        return self._json(200, {
            "ok": True,
            "files": files,
            "score_url": (f"/api/files/{pid}/out/{score.name}" if score else None),
            "report": report,
            "segments": segments,
            "duration_in": round(duration_in, 3),
            "warnings": warnings,
            "log": log_text,
            **final,
        })


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def _lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
        keep_temp: bool = False, temp_dir: str | None = None) -> int:
    if not STATIC_DIR.is_dir():
        print(f"  ✗ 找不到界面资源目录：{STATIC_DIR}")
        print("     这个包安装不完整（web/static 缺失）。")
        return 1

    root = Path(temp_dir) if temp_dir else Path(tempfile.mkdtemp(prefix="MIDI-PianoSwitcher_"))
    root.mkdir(parents=True, exist_ok=True)

    Handler.projects_root = root
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True

    url = f"http://{host}:{port}/"
    print()
    print("=" * 70)
    print("  MIDI-PianoSwitcher GUI —— MIDI → 纯钢琴版 · 可视化剪辑")
    print("=" * 70)
    print(f"  界面地址： {url}")
    print(f"  工作目录： {root}")
    ms = find_musescore()
    print(f"  渲染器：   {ms if ms else '✗ 没找到 MuseScore —— 无法出音频/PDF'}")
    print(f"  ffmpeg：   {shutil.which('ffmpeg') or '✗ 没找到（MP3 会降级、OGG 不可用）'}")
    print()
    print("  只监听本机，文件不会离开这台电脑。")
    print("  按 Ctrl+C 退出。")
    print("=" * 70)
    print()

    # 关掉的时候清理临时目录（除非明确要保留）
    def _cleanup():
        if not keep_temp:
            shutil.rmtree(root, ignore_errors=True)

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  正在退出…")
    finally:
        httpd.server_close()
        _cleanup()
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="MIDI-PianoSwitcher 图形界面（本地 Web UI，只监听本机）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址，默认只本机。填 0.0.0.0 可让同局域网设备访问")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--temp-dir", default=None, help="指定工作目录（默认系统临时目录）")
    ap.add_argument("--keep-temp", action="store_true",
                    help="退出时保留工作目录（排查问题用）")
    a = ap.parse_args(argv)
    return run(host=a.host, port=a.port, open_browser=not a.no_browser,
               keep_temp=a.keep_temp, temp_dir=a.temp_dir)


if __name__ == "__main__":
    sys.exit(main())
