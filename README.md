# pianoize

把任意 MIDI 转成**纯钢琴版** —— 一条命令同时产出**音频、MIDI、可打印的钢琴谱**。

```bash
pianoize --src song.mid --outdir out --pdf
```

```
【1/6】解剖       音轨 / 乐器 / 通道 / 音域 / 中位音高 + 重复轨两两相似度
【2/6】处理       删重复轨 + 统一钢琴音色 + 静音打击乐
【3/6】修重击     削短同音高零间隔的 note 尾部（消采样器咔哒声）
【4/6】写谱       分左右手 + 量化 + 按拍号精确分小节
【5/6】校验       逐声部检查 MusicXML 小节时值，拦住 MuseScore 会拒收的谱
【6/6】渲染       MuseScore → WAV → 响度标准化 → 限幅 → 任意格式导出
```

产物：`*_piano.mid` · `*.wav` / `*.flac` / `*.mp3` / `*.ogg` · `*.musicxml` · `*.pdf` · `*_report.json`

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

**可选的外部程序**（不装也能用，只是少功能）：

| 程序 | 用途 | 缺了会怎样 |
|---|---|---|
| **MuseScore 4** | 渲染音频、导 PDF | 跳过音频/PDF，MIDI 和 MusicXML 照常输出 |
| **ffmpeg** | MP3/OGG 导出；m4a 等格式解码 | MP3 退回 libsndfile（256k）；OGG 与 m4a 不可用 |

```bash
# Windows
winget install MuseScore.MuseScore ffmpeg
# macOS
brew install --cask musescore && brew install ffmpeg
```

## 用法

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

# 批量（串行，避免几百 MB 的临时文件叠加）
pianoize-batch --indir ./midis --outdir out --recursive --pdf
```

### 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
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

- **左右手分离是启发式**：单轨/音频转出来的 MIDI 只能按音高阈值分，原曲两手音域重叠时仍会分得不理想 → 用 `--split` 手动指定。
- **量化会损失演奏细节**：`--grid` 默认 0.25。三连音素材建议调细。
- **音频转谱准确度有限**：见上表，别期待能直接用。
- **钢琴谱是"可读"而非"可演奏"**：无法还原真实指法、踏板、声部走向，复杂曲目请当草稿。
- **长曲资源占用**：6 分钟曲子渲染约 20 秒、中间 WAV 约 127 MB。批量是串行的。
- **OGG 只走 ffmpeg**：libsndfile 写 OGG 在本项目测试环境下会**原生崩溃**（`0xC0000409` stack buffer overrun，异常捕获不住），所以绕开它。

## 测试

```bash
pip install -e ".[dev]"
pytest -q
```

22 个测试，**全部不依赖 MuseScore / ffmpeg**，所以 CI 上不装任何外部程序也能跑。
覆盖的重点是那几个护栏：坏谱必须被判坏（`test_validator_catches_voice_overflow`）、
好谱必须通过、长音不得撑爆小节、起音时刻不得被重击修复改动、中文轨名必须能写出去。

## 项目结构

```
src/pianoize/
  core.py            纯逻辑：诊断/改音色/修重击/写谱/校验/响度（可单独 import 测试）
  runner.py          编排：argparse + 六个阶段的调度
  batch.py           批量模式
  cli.py             console_scripts 入口
  audio2midi.py      音频 → MIDI（轻量频谱法）
  eval_audio2midi.py 转录准确度评估
tests/test_core.py   22 个测试
tools/make_stress_midi.py  生成带边界的合成测试文件
```

## 版权与免责

本工具只做**格式与音色转换**，不包含任何音乐内容。

- 自己练琴、学编配、私下听：没问题。
- **公开发布录音 / 上传改编谱 / 商用：需要另行取得原作品授权。**
- 仓库里唯一的音乐素材是 `tools/make_stress_midi.py` 生成的合成测试文件（本项目原创）。

MuseScore 是外部可选程序，不随本项目分发，其自身许可（GPL）独立适用。

## License

MIT —— 见 [LICENSE](LICENSE)。
