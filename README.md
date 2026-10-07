# MIDI-PianoSwitcher

> 我草这DS原来连README都帮我写好了，算了这样也挺好

把 MIDI 转成**纯钢琴版**：出音频、MIDI，还能出可打印的钢琴谱。
带一个网页界面，可以在波形上框选片段做剪辑（删掉 / 复制 / 升降调 / 调音量）。

**纯个人爱好项目，100% Made In DeepSeek。兼容性和性能不做保证。**

---

## 装

```bash
git clone https://github.com/misaka11451-blip/MIDI-PianoSwitcher
cd MIDI-PianoSwitcher
pip install -e .
```

还需要两个外部程序（管音频渲染和格式转换，不装的话会少功能）：

```bash
winget install MuseScore.MuseScore ffmpeg            # Windows
brew install --cask musescore && brew install ffmpeg # macOS
```

## 用

命令有长短两种写法，完全等价：`mps` ↔ `midi-pianoswitcher`。

### 图形界面（推荐）

```bash
mps-gui
```

会自动开浏览器（`http://127.0.0.1:8765`）。流程：
**拖入 MIDI → 波形上框选 → 选操作 → 加到列表 → 试听 → 完整渲染 → 下载**。

### 命令行

```bash
mps --src song.mid --outdir out --pdf
```

产物在 `out/` 里：`*_piano.mid`、`.mp3` / `.flac` / `.wav`、`.musicxml`、`.pdf`。

常用参数：

| 参数 | 用途 |
|---|---|
| `--pdf` | 顺带出 PDF 谱 |
| `--audio-format flac` | 改输出格式（`wav` `flac` `mp3` `ogg`，逗号分隔） |
| `--bitdepth 24` | 无损格式用 24bit |
| `--no-audio` | 只要 MIDI 和谱，快很多 |
| `--piano rhodes` | 换音色（`piano` `bright` `rhodes` `harpsichord` `clav`…） |
| `--edit 预设.json` | 带剪辑，见下 |

批量转换：`mps-batch --indir ./midis --outdir out --pdf`

## 剪辑

界面上框选一段，选一种操作：**删除 / 复制 / 升降音高 / 调整音量**。
复制可以选插到哪（紧跟其后 / 曲末 / 指定时间点）。

命令行用 JSON，也能存成预设文件反复用：

```bash
mps --src song.mid --outdir out --edit '[
  {"kind":"trim",      "start":10, "end":20},
  {"kind":"duplicate", "start":0,  "end":8, "repeat":2, "at":"end"},
  {"kind":"pitch",     "start":30, "end":45, "semitones":-12},
  {"kind":"volume",    "start":0,  "end":60, "db":-6}]'
```

> Windows 上 PowerShell 传内联 JSON 容易被引号搞乱，建议存成文件再 `--edit 文件.json`。

## 音频转 MIDI

```bash
mps-audio2midi --src song.mp3 --out song.mid
```

**准确度有限**，钢琴独奏实测 F1 约 0.45，别期待能直接用。
**有原曲 MIDI 就一定用 MIDI，不要从音频转。**

## 已知限制

- 左右手分离是启发式，两手音域重叠时会分不准 → 用 `--split` 手动指定
- 钢琴谱是"能读"而不是"能弹"，复杂曲目请当草稿
- 6 分钟的曲子渲染约 20 秒，中间文件约 127 MB
- 界面只监听本机、没有鉴权，**别暴露到公网**

## 版权

自己练琴、学编配、私下听没问题。
**公开发布录音 / 上传改编谱 / 商用需另行取得原作品授权。**

## License

MIT。开发过程中的踩坑与决策记录在 [docs/DESIGN.md](docs/DESIGN.md)。
