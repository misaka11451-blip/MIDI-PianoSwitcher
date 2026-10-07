"""从**源码目录**启动图形界面（不需要先 pip install）。

为什么需要这个脚本：直接在仓库根目录跑 ``python -m midi_pianoswitcher.webapp`` 会失败，
报 ``__path__ attribute not found on 'MIDI-PianoSwitcher'``。原因是仓库根目录**本身就叫
``MIDI-PianoSwitcher/``**，它会被当成一个没有 ``__init__.py`` 的命名空间包，**遮蔽掉
``src/midi_pianoswitcher`` 里真正的包**。

这个脚本做两件事就绕开了：
  1. 把 ``src/`` 放进 PYTHONPATH
  2. 把工作目录设成 ``src/``（而不是仓库根目录）

正常 ``pip install -e .`` 之后不需要它，直接用 ``midi-pianoswitcher-gui`` 命令即可。

用法：

    python tools/launch_gui.py                 # 默认 8765 端口
    python tools/launch_gui.py --port 9000
    python tools/launch_gui.py --no-browser
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从源码启动 MIDI-PianoSwitcher 图形界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--temp-dir", default=None,
                    help="工作目录（默认系统临时目录；给了就保留内容）")
    a = ap.parse_args(argv)

    if not (SRC / "midi_pianoswitcher" / "webapp.py").exists():
        print(f"  ✗ 找不到源码：{SRC / 'midi_pianoswitcher' / 'webapp.py'}")
        return 1

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"

    cmd = [sys.executable, "-m", "midi_pianoswitcher.webapp",
           "--host", a.host, "--port", str(a.port)]
    if a.no_browser:
        cmd.append("--no-browser")
    if a.temp_dir:
        cmd += ["--temp-dir", a.temp_dir, "--keep-temp"]

    url = f"http://{a.host}:{a.port}/"
    print(f"  启动中… 工作目录 {SRC}")
    proc = subprocess.Popen(cmd, env=env, cwd=str(SRC))

    # 等服务起来再决定要不要开浏览器（webapp 自己也会开，这里只是一层保险）
    ready = False
    for _ in range(40):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(url + "api/health", timeout=2):
                ready = True
                break
        except Exception:
            if proc.poll() is not None:
                break
    if ready:
        print(f"  ✓ 已就绪：{url}")
    else:
        print("  … 服务尚未就绪，请看上面的输出")

    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    sys.exit(main())
