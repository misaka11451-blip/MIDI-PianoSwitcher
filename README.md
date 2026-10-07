# pianoize

把任意 MIDI 转成**纯钢琴版** —— 一条命令同时产出**音频、MIDI、可打印的钢琴谱**，
还带一个**可视化剪辑界面**。

```bash
pianoize-gui                                    # 打开图形界面（推荐新手）
pianoize --src song.mid --outdir out --pdf      # 命令行
```

**图形界面**（`pianoize-gui`）：上传 MIDI → 看波形 → 框选区间 → 加剪辑操作 →
试听 → 完整渲染 → 下载。只监听本机，文件不出这台电脑。

**DIY 剪辑**（界面里点，命令行也能用）：

| 操作 | 作用 | 命令行 JSON |
|---|---|---|
| 删除片段 | 删掉 [起点, 终点) 这一段，后面的接上 | `{"kind":"trim","start":10,"end":20}` |
| 复制片段 | 把这一段再播 N 次（插在该段之后） | `{"kind":"duplicate","start":0,"end":8,"repeat":2}` |
| 升降音高 | 这一段整体升/降若干半音（±12 就是一个八度） | `{"kind":"pitch","start":30,"end":45,"semitones":-12}` |
| 调整音量 | 这一段整体增减若干 dB | `{"kind":"volume","start":0,"end":60,"db":-6}` |

操作按列表顺序依次施加（顺序有意义），可以存成 **JSON 预设**随时复用：

```bash
pianoize --src song.mid --outdir out --edit my_preset.json --pdf
pianoize --src song.mid --outdir out --edit my_preset.json --save-preset copy.json
```

```
【0/6】剪辑       施加 DIY 操作（可选，走 tempo map 换算时间轴）
【1/6】解剖       音轨 / 乐器 / 通道 / 音域 / 中位音高 + 重复轨两两相似度
【2/6】处理       删重复轨 + 统一钢琴音色 + 静音打击乐
【3/6】修重击     削短同音高零间隔的 note 尾部（消采样器咔哒声）
【4/6】写谱       分左右手 + 量化 + 按拍号精确分小节
【5/6】校验       逐声部检查 MusicXML 小节时值，拦住 MuseScore 会拒收的谱
【6/6】渲染       MuseScore → WAV → 响度标准化 → 限幅 → 任意格式导出
```

产物：`*_piano.mid` · `*.wav` / `*.flac` / `*.mp3` / `*.ogg` · `*.musicxml` · `*.pdf` · `*_report.json`

---

## 两个关键设计决定

### 1. 剪辑做在 MIDI 域，而不是剪音频波形

- **一次剪辑，三份产物都自动正确** —— 音频、MIDI、钢琴谱全都跟着变，
  不会出现"音频剪了但谱子还是旧的"这种不一致。
- **完全可复现** —— 操作清单能存成 JSON，随时重跑、随时改参数。
  剪过的波形是回不去的。
- **音高和音量在 MIDI 里是精确的**（音符号、velocity）；
  在音频域做要靠 phase vocoder 猜，必然有伪影。
- **时间轴走 tempo map** —— 变速曲子里"第 10 秒"对应的拍数不是常数，
  必须按速度变化分段积分。忽略这层是这类工具最常见的 bug：
  变速曲子上剪辑点会整体漂移。

界面上的**试听**会真的把剪辑后的 MIDI 渲染一遍再播给你听，不是假预览。

### 2. 图形界面用本地 Web UI，而不是 Qt/Tk

- **零新增重依赖**。界面由 Python 内置的 `http.server` 发出，渲染交给浏览器。
  对比：PySide6/PyQt 是 100+ MB 的依赖，塞进一个轻量工具并不合适。
- **浏览器是现成的渲染引擎**：波形、拖拽框选、音频播放（HTML5 audio）都不用自己写。
- **只监听 127.0.0.1**，不联网、不上传任何文件到外部。
- 界面资源零 CDN 引用，**完全离线可用**。

顺带踩掉两个真实兼容坑：`cgi` 模块从 Python 3.13 起已从标准库移除；
`email` 模块在 3.14 上会把中文文件名的原始字节替换成 U+FFFD（信息永久丢失）。
所以 multipart 解析是手写的，并有测试兜住。

---

## 为什么需要这个工具

"把 MIDI 换成钢琴音色"听起来是一行代码的事，实际操作会踩一串坑。这个项目把每个坑都变成了内置处理：

| 坑 | 症状 | 本项目的处理 | 实测数据 |
|---|---|---|---|
| **重复轨** | 同一段音乐叠了两遍，全改钢琴后变响、发糊 | 两两相似度检测，自动删掉 | 某文件轨0/轨1 相似度 **0.989**，响度差 **4.26 LU** |
| **同音高重击** | 前一个音刚 off 同一 tick 又 on，采样器波形不连续 → 咔哒声 | 只把前一个音的尾部提前，起音不动 | 单曲实测 **1370 处** |
| **打击乐通道** | ch9 混进钢琴，变成奇怪的敲击音 | 单独静音并摘掉空轨 | — |
| **小节被撑爆** | 谱面里某小节装了 4.25 拍 → **MuseScore 直接拒收**（返回码 1320） | 逐声部校验 + 按拍号重分小节 | 某 OMR 输出 **209 处**声部溢出 |
| **左右手反了** | 按轨序号分左右手，结果下声部给了右手 | 按音高阈值分，自动挑最平衡的界 | 实测确实会反 |
| **中文轨名** | MIDI 文本事件是 latin-1，写不出去 → 整个文件废掉 | 清洗成 ASCII | 实测轨名读出来是 `Òô¹ì 3` |

### 两个值得单独说的技术点

**1. 校验必须按「声部」而不是「整谱」**

这是实测逼出来的。某份坏谱子，按小节总时值算正好 4.00 拍（看起来合法），
但第 1 小节的 **voice 0 装了 0.25+2.00+2.00 = 4.25 拍** —— 这正是 MuseScore 拒收的原因。
只看小节总和会**漏报**，必须逐 voice 累加。`tests/test_core.py` 里
`test_validator_catches_voice_overflow` 固化了这个 case。

**2. 小节串行器的三条硬规则**

"撑爆"的成因是把长音的尾部挤到了后面的音上。所以写谱时：

- 每个音的时值 = `min(自己长度, 到下一个音起点的距离)`
- 只写落在本小节内、且在游标之后的音
- 空隙补休止，末尾补休止到小节**精确**结束（写完还断言一次）

同时两个 part 的小节数强制取齐 —— MusicXML 要求所有 part 小节数相同，否则拒收。

---

## 安装

```bash
git clone https://github.com/yourname/pianoize
cd pianoize
pip install -e .
```

依赖：`mido` `pretty_midi` `numpy` `soundfile` `scipy` `pyloudnorm`
（**图形界面不额外引入任何依赖** —— 只用 Python 标准库 + 浏览器）

装好后的四个命令：

| 命令 | 用途 |
|---|---|
| `pianoize-gui` | 图形界面（上传 → 剪辑 → 试听 → 渲染） |
| `pianoize` | 单文件转纯钢琴版 |
| `pianoize-batch` | 批量转换 |
| `pianoize-audio2midi` | 音频转 MIDI（准确度有限，见下文） |

**可选的外部程序**（不装也能用，只是少功能）：

| 程序 | 用途 | 缺了会怎样 |
|---|---|---|
| **MuseScore 4** | 渲染音频、导 PDF | 跳过音频/PDF，MIDI 和 MusicXML 照常输出 |
| **ffmpeg** | MP3/OGG 导出；m4a 等格式解码 | MP3 退回 libsndfile；OGG 与 m4a 不可用 |

```bash
# Windows
winget install MuseScore.MuseScore ffmpeg
# macOS
brew install --cask musescore && brew install ffmpeg
```

## 用法

### 图形界面

```bash
pianoize-gui
```

会启动一个本地服务并自动打开浏览器（默认 `http://127.0.0.1:8765`）。
界面上：拖入 MIDI → 在波形上拖动框选区间 → 选操作类型（删除/复制/升降调/调音量）
→ 加到列表 → 试听 → 完整渲染 → 下载。

常用参数：

```bash
pianoize-gui --port 9000        # 换端口
pianoize-gui --no-browser       # 不自动开浏览器
pianoize-gui --host 0.0.0.0     # 让同局域网设备也能访问（默认只本机）
```

### 命令行

```bash
# 最简
pianoize --src song.mid --outdir out

# 要 PDF 谱
pianoize --src song.mid --outdir out --pdf

# 无损 24bit FLAC
pianoize --src song.mid --outdir out --audio-format flac --bitdepth 24

# 四种格式一起出
pianoize --src song.mid --outdir out --audio-format wav,flac,mp3,ogg

# 只要 MIDI 和谱，不渲染（快很多）
pianoize --src song.mid --outdir out --no-audio --pdf

# 带剪辑：删 10–20 秒，再把开头 8 秒重复两次
pianoize --src song.mid --outdir out --edit '[
  {"kind":"trim","start":10,"end":20},
  {"kind":"duplicate","start":0,"end":8,"repeat":2}]'

# 批量（串行，避免几百 MB 的临时文件叠加）
pianoize-batch --indir ./midis --outdir out --recursive --pdf
```

> Windows 上用 PowerShell 传内联 JSON 容易被引号规则吃掉，**建议存成
> 预设文件再用 `--edit 文件.json`**。

### 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--edit` | 无 | 剪辑操作：预设 JSON 路径或内联 JSON 数组 |
| `--save-preset` | 无 | 把 `--edit` 的操作清单另存为预设 |
| `--export-edited-midi` | 关 | 额外输出「剪辑后但未转钢琴」的 MIDI，便于核对 |
| `--piano` | `piano` | 音色：`piano` `bright` `rhodes` `harpsichord` `clav` … |
| `--drop-dup` | `auto` | `auto` / `none` / `2,3`（手动指定要删的轨号） |
| `--retrigger-ms` | `25` | 同音高最小间隙；`0` = 不修 |
| `--split` | 自动 | 左右手分界音高 |
| `--time` / `--bpm` | 读 MIDI | 拍号 / 速度覆盖 |
| `--grid` | `0.25` | 量化网格（四分音符单位）。三连音素材用 `0.0625` 或 `1/12` |
| `--audio-format` | `wav,mp3` | `wav` `flac` `mp3` `ogg` 任意组合 |
| `--bitdepth` | `16` | 无损格式位深：`16` / `24` |
| `--lufs` | `-14` | 目标响度（流媒体 -14，CD -9～-12） |

## 音频 → MIDI（可选，且请先读这段）

```bash
python -m pianoize.audio2midi --src song.mp3 --out song.mid
```

**音频转 MIDI 是信息有损的**：波形里没有"乐器、时值、力度、拍号"这些信息，全靠推测。
本项目实现了一个轻量频谱法（CQT 峰值 + 泛音抑制 + 帧聚类），**不需要任何深度学习依赖**。

实测准确度（note-level，音高必须相同且起音落在 ±0.1s 内）：

| 素材 | 精确率 | 召回率 | F1 |
|---|---|---|---|
| 钢琴独奏，**且音频由 MIDI 干净渲染**（等于上限测试） | 31.6% | 78.7% | **0.45** |
| 真实录音（钢琴翻弹 m4a） | — | — | 约 0.3–0.4 |

对照：专用的深度模型（如 Basic Pitch）在钢琴独奏上 F1 通常能到 0.7–0.9，
代价是拖进 TensorFlow（数百 MB）。本项目刻意不这么做，保持零重依赖。

```bash
# 自己量化一下准确度：拿已知真值的 MIDI 渲染成音频，再转回来对答案
python -m pianoize.eval_audio2midi --midi truth.mid --audio truth.wav
```

> **如果你的目标是"纯钢琴版"，最优解永远是找到原曲的 MIDI，而不是从音频转。**
> 音频转谱只在"实在没有 MIDI"时当草稿用。

## 已知限制

- **剪辑在 MIDI 域做**：所以音量和音高的改动精度受 MIDI 本身限制 ——
  velocity 只有 1–127，`+20 dB` 这种大幅增益会被钳住（界面的执行报告会如实
  告诉你钳了几个音）；移调超出音域的音也会被钳住、旋律会变形。
  要真正做大幅增益，得在渲染出的音频上再处理。
- **左右手分离是启发式**：单轨/音频转出来的 MIDI 只能按音高阈值分，原曲两手音域重叠时仍会分得不理想 → 用 `--split` 手动指定。
- **量化会损失演奏细节**：`--grid` 默认 0.25。三连音素材建议调细。
- **音频转谱准确度有限**：见上表，别期待能直接用。
- **钢琴谱是"可读"而非"可演奏"**：无法还原真实指法、踏板、声部走向，复杂曲目请当草稿。
- **长曲资源占用**：6 分钟曲子渲染约 20 秒、中间 WAV 约 127 MB。批量是串行的。
- **图形界面只支持 MIDI 输入**：音频要先经 `pianoize-audio2midi` 转一遍
  （准确度有限）。这是刻意的 —— 与其在界面里给个不靠谱的按钮，不如说明白。
- **OGG 只走 ffmpeg**：libsndfile 写 OGG 在本项目测试环境下会**原生崩溃**（`0xC0000409` stack buffer overflow，异常捕获不住），所以绕开它。
- **界面是单机自用设计**：`/api/*` 没有鉴权，任何人能访问就能上传/渲染。
  默认只监听 `127.0.0.1`；**不要把它直接暴露到公网**。

## 测试

```bash
pip install -e ".[dev]"
pytest -q
```

76 个测试。纯逻辑的测试**不依赖 MuseScore / ffmpeg**，CI 上不装任何外部程序也能跑；
涉及真实渲染的用例在没装 MuseScore 时会自动 skip。

覆盖的重点是那些护栏和踩过的坑：

| 测试 | 守住什么 |
|---|---|
| `test_validator_catches_voice_overflow` | 坏谱必须被判坏（只看小节总时值会漏报） |
| `test_measure_content_exactly_fills_bar` | 每小节时值必须精确等于小节长度 |
| `test_long_note_does_not_overflow_measure` | 长音不得撑爆后续小节 |
| `test_retrigger_fix_moves_only_note_end` | 修重击不得改变起音时刻 |
| `test_chinese_track_name_survives_write` | 中文/乱码轨名必须能写出去 |
| `test_tempo_map_with_tempo_change` | 变速曲子的时间轴换算 |
| `test_order_matters` | 剪辑操作顺序真的会改变结果 |
| `test_pitch_clamps_out_of_range_and_reports_it` | 钳位必须如实汇报 |
| `test_multipart_decodes_utf8_chinese_filename` | 中文文件名不能被替换成 U+FFFD |
| `test_static_path_traversal_blocked` | 界面资源不能被路径穿越读取 |

## 项目结构

```
src/pianoize/
  core.py            纯逻辑：诊断/改音色/修重击/写谱/校验/响度（可单独 import 测试）
  ops.py             DIY 剪辑：trim/duplicate/pitch/volume + tempo map + 预设 JSON
  runner.py          编排：argparse + 七个阶段的调度
  batch.py           批量模式
  webapp.py          图形界面后端（内置 http.server，含手写 multipart 解析）
  web/static/        图形界面前端：index.html + app.css + app.js（零 CDN）
  audio2midi.py      音频 → MIDI（轻量频谱法）
  eval_audio2midi.py 转录准确度评估
tests/
  test_core.py       核心流水线（含乐谱校验护栏）
  test_ops.py        DIY 剪辑（含变速时间轴、钳位、预设往返）
  test_timeline.py   原始↔处理后 时间轴映射（播放头对齐靠它）
  test_web.py        multipart 解析 + HTTP 接口端到端
tools/
  make_stress_midi.py  生成带边界的合成测试文件（本项目原创，可安全分发）
  smoke_web_api.py     GUI 接口端到端冒烟测试（需服务在跑）
  lint_frontend.py     前端静态校验：JS 语法 + DOM id 一致性 + 零外部依赖
```

想知道界面是否接线正确、又懒得开浏览器时：

```bash
pianoize-gui --no-browser --port 8765     # 另开一个终端窗口
python tools/smoke_web_api.py http://127.0.0.1:8765 song.mid ./out
python tools/lint_frontend.py
```

## 版权与免责

本工具只做**格式与音色转换**，不包含任何音乐内容。

- 自己练琴、学编配、私下听：没问题。
- **公开发布录音 / 上传改编谱 / 商用：需要另行取得原作品授权。**
- 仓库里唯一的音乐素材是 `tools/make_stress_midi.py` 生成的合成测试文件（本项目原创）。

MuseScore 是外部可选程序，不随本项目分发，其自身许可（GPL）独立适用。

## License

MIT —— 见 [LICENSE](LICENSE)。
