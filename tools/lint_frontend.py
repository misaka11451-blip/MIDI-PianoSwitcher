"""静态校验前端：JS 语法 + DOM id 引用一致性 + 基本结构。

没有浏览器可用，所以用这种办法尽量在交付前抓出明显的接线错误。
（CI 上可以直接跑；找不到 node 时会跳过语法检查那一项。）

用法：

    python tools/lint_frontend.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
R = Path(__file__).resolve().parents[1]
S = R / "src" / "pianoize" / "web" / "static"
NODE = shutil.which("node")

html = (S / "index.html").read_text(encoding="utf-8", errors="replace")
js = (S / "app.js").read_text(encoding="utf-8", errors="replace")
css = (S / "app.css").read_text(encoding="utf-8", errors="replace")

print("=" * 70)
print("  前端静态校验")
print("=" * 70)

# ---------- 1) JS 语法 ----------
print("\n【1】JS 语法检查（用 Node --check）")
if not NODE:
    print("  – 没找到 node，跳过语法检查（其余检查继续）")
    r = None
else:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(js)
        js_tmp = Path(tf.name)
    r = subprocess.run([str(NODE), "--check", str(js_tmp)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
if r is None:
    pass
elif r.returncode == 0:
    print("  ✓ 语法正确")
else:
    print("  ✗ 语法错误：")
    print("   ", (r.stderr or "").strip()[:800])

# ---------- 2) DOM id 一致性 ----------
print("\n【2】DOM id 引用一致性")
html_ids = set(re.findall(r'\bid="([^"]+)"', html))
js_ids = set(re.findall(r'getElementById\(\s*["\']([^"\']+)["\']', js))
js_ids |= set(re.findall(r'querySelector\(\s*["\']#([A-Za-z0-9_\-]+)["\']', js))
# 前端用了 `const $ = id => document.getElementById(id)` 这种包装，也要算进去
js_ids |= set(re.findall(r'\$\(\s*["\']([A-Za-z0-9_\-]+)["\']\s*\)', js))
missing = sorted(js_ids - html_ids)
print(f"  HTML 里定义了 {len(html_ids)} 个 id；JS 引用了 {len(js_ids)} 个")
if missing:
    print(f"  ✗ JS 引用了 HTML 里不存在的 id（{len(missing)} 个）：")
    for m in missing[:40]:
        print(f"      {m}")
else:
    print("  ✓ JS 引用的 id 全部存在于 HTML")

unused = sorted(html_ids - js_ids)
if unused:
    print(f"  （HTML 里未被 JS 直接引用的 id {len(unused)} 个，通常是纯样式钩子）")
    print("   ", ", ".join(unused[:20]))

# ---------- 3) 事件绑定与关键函数 ----------
print("\n【3】关键交互是否有实现")
checks = {
    "拖拽上传（dragover/drop）": r"dragover|drop",
    "波形绘制": r"getContext\(|fillRect|clearRect",
    "播放头/seek": r"seek|currentTime",
    "框选区间": r"mousedown|mousemove|mouseup|pointerdown",
    "高分屏适配": r"devicePixelRatio",
    "fetch 封装": r"async function|fetch\(",
    "错误提示": r"catch\s*\(",
    "预设导出": r"createObjectURL|download",
    "预设导入": r"FileReader|readAsText",
    "操作上移/下移": r"上移|下移|moveUp|moveDown|shift",
    "预计时长变化": r"delta|shift|预计",
}
for label, pat in checks.items():
    print(f"  {'✓' if re.search(pat, js) else '✗'} {label}")

# ---------- 4) CSS 结构 ----------
print("\n【4】CSS 结构")
print(f"  行数 {len(css.splitlines())}；规则块约 {css.count('{')} 个")
print(f"  {'✓' if ':root' in css else '✗'} 有 CSS 变量（:root）")
print(f"  {'✓' if '@media' in css else '✗'} 有响应式断点（@media）")
braces_ok = css.count("{") == css.count("}")
print(f"  {'✓' if braces_ok else '✗'} 花括号配对（{{={css.count('{')} }}={css.count('}')}）")

# ---------- 5) 必须没有的东西 ----------
print("\n【5】约束检查")
print(f"  {'✓' if 'http://' not in html and 'https://' not in html and 'http://' not in js and 'https://' not in js else '✗'} 无外部 URL 依赖")
# 只统计真正的 CDN 引用（src=/href= 指向 http(s)），注释里出现"CDN"不算
cdn_refs = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', html + js)
print(f"  {'✓' if not cdn_refs else '✗'} 无 CDN 引用（src/href 指向外部）"
      + (f"  {cdn_refs}" if cdn_refs else ""))
inline_script = re.findall(r"<script(?![^>]*src=)[^>]*>", html)
print(f"  {'✓' if not inline_script else '（注意）'} 无内联 <script>（{len(inline_script)} 个）")
local_refs = re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', html)
print(f"  本地引用: {local_refs}")

# ---------- 6) [hidden] 兜底规则 + display 覆盖检查 ----------
# 这个坑真实发生过：.busy / .result-files 等写了 display:flex，
# 作者样式盖掉了浏览器对 [hidden] 的默认 display:none，
# 于是 JS 里 hidden=true 设了也没用 —— 表现就是"渲染完了还在转圈、
# 结果区永远不出现"。所以这里做双重检查，防止它被误删。
print("\n【6】hidden 属性是否真的生效")
has_guard = bool(re.search(r"\[hidden\][^{]*\{[^}]*display\s*:\s*none", css))
print(f"  {'✓' if has_guard else '✗'} 有 [hidden] {{ display: none }} 兜底规则"
      + ("" if has_guard else "  ← 缺了它，任何 display 规则都会盖掉 hidden"))

hidden_ids = re.findall(r'id="([^"]+)"[^>]*\shidden(?:\s|>)', html)
overridden = []
for eid in hidden_ids:
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        sel = " ".join(m.group(1).split())
        if eid not in sel or "[hidden]" in sel:
            continue
        d = re.search(r"display\s*:\s*([a-zA-Z\-]+)", m.group(2))
        if d and d.group(1) != "none":
            overridden.append((eid, sel[-40:], d.group(1)))
if overridden and not has_guard:
    print(f"  ✗ {len(overridden)} 个元素会被 display 规则盖掉 hidden，且没有兜底：")
    for eid, sel, d in overridden:
        print(f"      #{eid}  {sel} → display:{d}")
elif overridden:
    print(f"  ✓ {len(overridden)} 个元素设了 display，但兜底规则会压住它们：")
    for eid, sel, d in overridden:
        print(f"      #{eid} → display:{d}")
else:
    print("  ✓ 没有元素存在被覆盖的风险")

print()
print("=" * 70)
