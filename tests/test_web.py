"""Web 界面（GUI）的测试。

分成两块：

  1. **multipart 解析** —— 不能用 ``cgi``（Python 3.13 起已从标准库移除），
     也不能用 ``email``（3.14 上会把中文文件名的原始字节替换成 U+FFFD，
     信息永久丢失）。所以是手写字节解析，必须有测试兜住。
  2. **HTTP 接口** —— 用真实的 ThreadingHTTPServer 起在随机端口上跑一遍，
     覆盖上传/试听/渲染/下载/各种错误路径。

**不依赖 MuseScore**：涉及渲染的用例在没有 MuseScore 时会跳过，
所以 CI 上不装外部程序也能跑。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from midi_pianoswitcher import webapp  # noqa: E402


# --------------------------------------------------------------------------- #
# 造一个最小但合法的 MIDI（含 3/4 拍、重复轨、鼓轨、重击）
# --------------------------------------------------------------------------- #
def make_test_midi(n_notes: int = 40) -> bytes:
    """造一个最小但合法的 MIDI。

    轨数/内容刻意选成这样，好让流水线的每个分支都有东西可测：
      * 轨1 与轨2 内容完全相同 → 重复轨检测应命中
      * 轨3 走 ch9 → 打击乐应被识别并静音
      * 3/4 拍 → 非 4/4 的分小节与校验
      * 音符之间零间隔 → 重击修复应命中

    注意 n_notes 不能太小：``analyze`` 里有个**防误报门槛**，
    音符数少于 max(20, 总音符数的 2%) 的轨不参与重复比对（与鼓轨比对时门槛更高）。
    太短的样本会一个重复都测不出来 —— 这是特性不是 bug，
    所以这里默认给 40 个音，让它稳稳超过门槛。
    """
    def vlq(n: int) -> bytes:
        out = [n & 0x7F]
        n >>= 7
        while n:
            out.append((n & 0x7F) | 0x80)
            n >>= 7
        return bytes(reversed(out))

    tpb = 480

    def track(events: bytes) -> bytes:
        return b"MTrk" + len(events).to_bytes(4, "big") + events

    # 轨0：速度 + 拍号
    e0 = bytearray()
    e0 += vlq(0) + b"\xff\x03\x03" + b"Con"
    e0 += vlq(0) + b"\xff\x58\x04\x03\x02\x18\x08"      # 3/4
    e0 += vlq(0) + b"\xff\x51\x03" + (500000).to_bytes(3, "big")
    e0 += vlq(0) + b"\xff\x2f\x00"

    # 轨1：钢琴旋律（零间隔重击）
    e1 = bytearray()
    e1 += vlq(0) + b"\xff\x03\x05" + b"Piano"
    e1 += vlq(0) + b"\xc0\x00"
    for i in range(n_notes):
        e1 += vlq(0 if i == 0 else 240) + bytes([0x90, 60 + (i % 12), 80])
        e1 += vlq(240) + bytes([0x80, 60 + (i % 12), 0])
    e1 += vlq(0) + b"\xff\x2f\x00"

    # 轨2：与轨1 完全相同的重复轨（不同通道、不同音色）
    e2 = bytearray()
    e2 += vlq(0) + b"\xff\x03\x03" + b"Dup"
    e2 += vlq(0) + b"\xc1\x0b"
    for i in range(n_notes):
        e2 += vlq(0 if i == 0 else 240) + bytes([0x91, 60 + (i % 12), 80])
        e2 += vlq(240) + bytes([0x81, 60 + (i % 12), 0])
    e2 += vlq(0) + b"\xff\x2f\x00"

    # 轨3：打击乐 ch9
    e3 = bytearray()
    e3 += vlq(0) + b"\xff\x03\x05" + b"Drums"
    for i in range(n_notes):
        e3 += vlq(0 if i == 0 else 480) + bytes([0x99, 36, 100])
        e3 += vlq(60) + bytes([0x89, 36, 0])
    e3 += vlq(0) + b"\xff\x2f\x00"

    header = (b"MThd" + (6).to_bytes(4, "big") + (1).to_bytes(2, "big")
              + (4).to_bytes(2, "big") + tpb.to_bytes(2, "big"))
    return header + track(bytes(e0)) + track(bytes(e1)) + track(bytes(e2)) + track(bytes(e3))


@pytest.fixture
def midi_bytes() -> bytes:
    return make_test_midi()


# --------------------------------------------------------------------------- #
# 1. multipart 解析
# --------------------------------------------------------------------------- #
def _mp(field: str, filename: bytes, payload: bytes,
        boundary: str = "----MIDI-PianoSwitcherTest") -> tuple[bytes, str]:
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="' + field.encode() + b'"; filename="' + filename + b'"\r\n'
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += payload
    body += f"\r\n--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def test_multipart_keeps_binary_intact(midi_bytes):
    body, ct = _mp("file", b"song.mid", midi_bytes)
    parts = webapp.parse_multipart(body, ct)
    assert set(parts) == {"file"}
    assert parts["file"]["data"] == midi_bytes, "二进制被破坏了"
    assert parts["file"]["filename"] == "song.mid"


def test_multipart_decodes_utf8_chinese_filename():
    """浏览器发中文文件名时 header 里是原始 UTF-8 字节，必须正确还原。

    这是踩过的坑：email 模块在 Python 3.14 上会把这些字节替换成 U+FFFD
    （'������.mid'），信息永久丢失。所以改成了手写解析。
    """
    body, ct = _mp("file", "测试曲目.mid".encode("utf-8"), b"MThd1234")
    parts = webapp.parse_multipart(body, ct)
    assert parts["file"]["filename"] == "测试曲目.mid", parts["file"]["filename"]
    assert parts["file"]["data"] == b"MThd1234"


def test_multipart_supports_rfc5987_filename_star():
    boundary = "----b"
    body = (f"--{boundary}\r\n".encode()
            + b"Content-Disposition: form-data; name=\"file\"; "
              b"filename*=UTF-8''%E6%B5%8B%E8%AF%95.mid\r\n\r\n"
            + b"MThd" + f"\r\n--{boundary}--\r\n".encode())
    parts = webapp.parse_multipart(body, f"multipart/form-data; boundary={boundary}")
    assert parts["file"]["filename"] == "测试.mid"


def test_multipart_handles_extra_fields():
    """除了 file，还可能有别的字段（表单里的选项）都要能解析出来。"""
    boundary = "----b"
    body = bytearray()
    for name, val in (("piano", b"rhodes"), ("lufs", b"-14")):
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += val + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="file"; filename="a.mid"\r\n\r\n'
    body += b"MThdDATA" + f"\r\n--{boundary}--\r\n".encode()
    parts = webapp.parse_multipart(bytes(body),
                                   f"multipart/form-data; boundary={boundary}")
    assert parts["piano"]["data"] == b"rhodes"
    assert parts["lufs"]["data"] == b"-14"
    assert parts["file"]["data"] == b"MThdDATA"


def test_multipart_rejects_missing_boundary():
    with pytest.raises(ValueError):
        webapp.parse_multipart(b"whatever", "multipart/form-data")


def test_multipart_ignores_quoted_boundary():
    body, ct = _mp("file", b"x.mid", b"MThd",
                   boundary="----quoted")
    parts = webapp.parse_multipart(body, ct.replace("----quoted", '"----quoted"'))
    assert parts["file"]["data"] == b"MThd"


# --------------------------------------------------------------------------- #
# 2. HTTP 接口（真实起服务）
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def server(tmp_path):
    if not webapp.STATIC_DIR.is_dir():
        pytest.skip("界面静态资源缺失，跳过 HTTP 测试")
    port = _free_port()
    webapp.Handler.projects_root = tmp_path
    httpd = ThreadingHTTPServer(("127.0.0.1", port), webapp.Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    yield base
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=5)


def _post_json(base: str, path: str, payload: dict):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _post_file(base: str, filename: str, content: bytes):
    body, ct = _mp("file", filename.encode("utf-8"), content,
                   boundary="----" + uuid.uuid4().hex)
    req = urllib.request.Request(base + "/api/project", data=body,
                                 headers={"Content-Type": ct}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_health_endpoint(server):
    st, raw = _get(server, "/api/health")
    assert st == 200
    body = json.loads(raw)
    assert body["ok"] is True
    assert "musescore" in body and "ffmpeg" in body


def test_index_served(server):
    st, raw = _get(server, "/")
    assert st == 200
    html = raw.decode("utf-8")
    assert "<canvas" in html and "app.js" in html and "app.css" in html
    assert "http://" not in html.split("<body")[0].replace("http://www.w3.org", "")


def test_static_assets_served(server):
    for name, needle in (("app.js", "addEventListener"), ("app.css", "{"),):
        st, raw = _get(server, "/" + name)
        assert st == 200, name
        assert needle in raw.decode("utf-8"), name


def test_static_path_traversal_blocked(server):
    st, _ = _get(server, "/../webapp.py")
    assert st in (403, 404)
    st, _ = _get(server, "/..%2f..%2fwebapp.py")
    assert st in (403, 404)


def test_upload_project(server, midi_bytes):
    st, proj = _post_file(server, "测试曲目.mid", midi_bytes)
    assert st == 200, proj
    assert proj["ok"] is True
    assert proj["filename"] == "测试曲目.mid"
    assert proj["duration"] > 0
    assert proj["n_notes"] > 0
    assert proj["time_signature"] == "3/4"
    assert len(proj["peaks"]) == 900
    assert all(0.0 <= p <= 1.0 for p in proj["peaks"])
    assert max(proj["peaks"]) > 0
    # 应检测出重复轨和鼓轨
    assert len(proj["duplicate_tracks"]) >= 1
    assert len(proj["drum_tracks"]) >= 1
    assert proj["warnings"]


def test_upload_rejects_non_midi(server):
    st, body = _post_file(server, "evil.txt", b"not a midi at all")
    assert st == 400
    assert "MIDI" in body["error"]


def test_upload_rejects_corrupt_midi(server):
    st, body = _post_file(server, "broken.mid", b"MThd\x00\x00\x00\x06garbage")
    assert st == 400
    assert not body["ok"]


def test_upload_rejects_empty(server):
    st, body = _post_file(server, "empty.mid", b"")
    assert st == 400


def test_download_rejects_bad_project_id(server):
    st, _ = _get(server, "/api/files/notahexid/file.mp3")
    assert st == 400
    st, _ = _get(server, "/api/files/" + "a" * 32 + "/file.mp3")
    assert st == 400          # 项目不存在


def test_download_blocks_subdir_outside_whitelist(server, midi_bytes):
    _, proj = _post_file(server, "a.mid", midi_bytes)
    pid = proj["project_id"]
    st, _ = _get(server, f"/api/files/{pid}/secret/file.txt")
    assert st == 403
    st, _ = _get(server, f"/api/files/{pid}/out/../../project.json")
    assert st in (400, 403, 404)


def test_download_blocks_hidden_file(server, midi_bytes):
    _, proj = _post_file(server, "a.mid", midi_bytes)
    st, _ = _get(server, f"/api/files/{proj['project_id']}/.project.json")
    assert st == 403


def test_project_json_persisted(server, midi_bytes, tmp_path):
    _, proj = _post_file(server, "a.mid", midi_bytes)
    f = tmp_path / proj["project_id"] / "project.json"
    assert f.exists()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["filename"] == "a.mid"
    # 关键：project.json 必须能被 json 序列化（numpy 标量曾让它崩过）
    assert isinstance(data["tracks"][0]["program"], int)


def test_preview_rejects_bad_operations(server, midi_bytes):
    _, proj = _post_file(server, "a.mid", midi_bytes)
    pid = proj["project_id"]
    for ops, why in (
        ([{"kind": "explode", "start": 1, "end": 2}], "未知类型"),
        ([{"kind": "trim", "start": 9, "end": 1}], "end<start"),
        ([{"kind": "trim", "start": 1, "end": 1}], "零长度"),
        ("not a list", "不是数组"),
        ([{"kind": "trim", "start": 1, "end": 2, "bogus": 1}], "多余字段"),
    ):
        st, body = _post_json(server, "/api/preview",
                              {"project_id": pid, "operations": ops})
        assert st == 400, f"{why} 应该被拒绝，实际 {st} {body}"
        assert body["ok"] is False
        assert body["error"]


def test_preview_unknown_project(server):
    st, body = _post_json(server, "/api/preview",
                          {"project_id": "0" * 32, "operations": []})
    assert st == 400
    assert "项目" in body["error"]


def test_unknown_route_404(server):
    st, _ = _get(server, "/api/does-not-exist")
    assert st == 404
    st, body = _post_json(server, "/api/nope", {})
    assert st == 404


def test_preview_applies_operations(server, midi_bytes):
    """试听要真实施加剪辑：时长按预期改变，并给出执行报告。"""
    from midi_pianoswitcher.core import find_musescore
    if not find_musescore():
        pytest.skip("没有 MuseScore，渲染类用例跳过")

    _, proj = _post_file(server, "a.mid", midi_bytes)
    pid = proj["project_id"]
    dur = proj["duration"]

    # 删掉中间一段（取曲子前 40%）
    a, b = dur * 0.3, dur * 0.7
    st, prev = _post_json(server, "/api/preview", {
        "project_id": pid,
        "operations": [{"kind": "trim", "start": a, "end": b},
                       {"kind": "pitch", "start": 0.0, "end": min(0.5, dur),
                        "semitones": 12}],
    })
    assert st == 200, prev
    assert prev["ok"]
    assert abs(prev["duration"] - (dur - (b - a))) < 0.3, (prev["duration"], dur)
    assert len(prev["report"]) == 2
    assert prev["report"][0]["kind"] == "trim"
    assert prev["report"][0]["删除音符"] >= 0
    assert prev["report"][1]["移调音符"] >= 0

    # 下载试听音频
    st, raw = _get(server, prev["audio_url"])
    assert st == 200
    assert len(raw) > 1000
    assert raw[:3] == b"ID3" or raw[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


def test_render_produces_expected_files(server, midi_bytes, tmp_path):
    from midi_pianoswitcher.core import find_musescore
    if not find_musescore():
        pytest.skip("没有 MuseScore，渲染类用例跳过")

    _, proj = _post_file(server, "a.mid", midi_bytes)
    pid = proj["project_id"]
    st, ren = _post_json(server, "/api/render", {
        "project_id": pid,
        "operations": [{"kind": "volume", "start": 0.0, "end": 0.3, "db": -6}],
        "audio_format": ["mp3", "flac"], "bitdepth": 24,
        "want_score": True, "want_pdf": True,
    })
    assert st == 200, ren
    assert ren["ok"]
    names = [f["name"] for f in ren["files"]]
    assert any(n.endswith(".mp3") for n in names), names
    assert any(n.endswith(".flac") for n in names), names
    assert any(n.endswith(".mid") for n in names), names
    assert ren["score_url"]

    # 每个产物都要能下载且大小对得上
    for f in ren["files"]:
        st2, raw = _get(server, f["url"])
        assert st2 == 200, f["name"]
        assert len(raw) == f["size"], f["name"]
    st3, pdf = _get(server, ren["score_url"])
    assert st3 == 200 and pdf[:4] == b"%PDF"
