# examples

这个目录**故意不包含任何音乐文件**。

原因有两层：

1. **版权**：真实的歌曲 MIDI / 录音都是版权作品，随仓库分发不合适。
   本工具的输出（WAV、FLAC、MP3、MusicXML、PDF）都是原作品的衍生品，
   同理不放进仓库 —— 见 `.gitignore` 里把音频和乐谱产物全部排除。
2. **必要性**：压测需要的是"已知答案的素材"，而最可控的做法是现场生成。

所以这里只放一条命令。它会造出一份带各种边界的合成 MIDI
（3/4 拍、鼓组、重复轨、刻意重击、窄音域、超长音），
用它就能把整条流水线走一遍：

```bash
python tools/make_stress_midi.py .
mps --src stress.mid --outdir out --pdf --audio-format wav,flac,mp3
```

想验证音频转谱的准确度，再用这份 MIDI 渲染出的音频对答案：

```bash
# 先用 MuseScore 把 stress.mid 渲染成 wav，然后：
python -m midi_pianoswitcher.eval_audio2midi --midi stress.mid --audio stress.wav
```

## 一点建议

如果你要拿自己的曲子试，优先用**原曲 MIDI**。没有 MIDI 再从音频转 ——
准确度差距很大（README 里有实测数字）。
