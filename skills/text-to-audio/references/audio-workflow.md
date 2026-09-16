# 配音命令与输入契约

脚本基于本播客项目原有适配层提取，随 skill 自带，不依赖原项目代码。Python 3.9+，依赖 `edge-tts`；FFmpeg/ffprobe 必须在 PATH 且含 libmp3lame。已做本地离线验证的依赖为 edge-tts 7.2.8，版本不表示最新或在线一定可用。

优先用当前可用环境；本机可检查 `/Users/dongyun/Documents/播客/.venv/bin/python`。其他机器在工作目录虚拟环境安装 `edge-tts==7.2.8`，不修改全局 Python。脚本使用 POSIX 文件锁，适用 macOS/Linux。

以下以 shell 变量 `AUDIO_PY`、`AUDIO_SKILL`、`AUDIO_OUT` 表示实际 Python、技能目录和输出目录。由当前环境解析这些路径，所有变量引用均加双引号。

## 文稿与选择文件

先原样保存用户稿件副本。规范化的 `podcast_script.md` 示例：

```markdown
# 文稿标题
## 正文
朗读者：这里是第一段正文。[短停顿]这句话有专有名词 Agent。
朗读者：这里是第二段正文。
## 配音备注
这里的说明不朗读。
```

多人稿用实际角色标签，映射设置中所有已出现的角色。仅在自然句界拆长段；避免整章一个请求。不要静默删掉正文中的列表、表格、链接内容；转换为可读语句，保留含义，无法判断应朗读哪些内容时询问。方括号中的制作标记会被提取而不朗读，原文确需读出的方括号内容要改为明确台词并记录。

停顿：换人 250 毫秒，换章节 800 毫秒，同角色连续段 0；显式标记优先，不累加。`[短停顿]`=250、`[停顿]`=800、`[停顿 500 毫秒]`=500。最后一段仅保留显式停顿。语气注释不执行为 SSML，也不保证产生指定情绪。

`audio_settings.json` 示例，**必须替换为用户实际选择**：

```json
{
  "speakers": {
    "reader": {
      "label": "朗读者",
      "voice": "zh-CN-YunyangNeural",
      "style_label": "淳厚男声",
      "rate": "+0%",
      "pitch": "+0Hz"
    }
  },
  "selection_basis": "本次用户选择：淳厚男声；仅生成音频",
  "cover": false,
  "require_script_gate": false,
  "duration_range_seconds": null
}
```

`cover`：true=要封面，false=不要，null=已发问尚未回复。音色必须已确认才能准备合成；封面可以稍后选择，最终选择写 `cover_record.json`，不改已锁定的音频设置。`require_script_gate` 依据当前项目规范填写；在本播客项目使用 true、时长范围 `[720,1080]`，用户明确另有要求时从其要求。普通文章不强制独立播客编辑或 15 分钟。

## 准备、核对与生成

```bash
"$AUDIO_PY" "$AUDIO_SKILL/scripts/podcast_audio.py" --help
"$AUDIO_PY" "$AUDIO_SKILL/scripts/verify_audio.py" --help
"$AUDIO_PY" "$AUDIO_SKILL/scripts/podcast_audio.py" --manifest "$AUDIO_OUT/audio_manifest.json" --prepare "$AUDIO_OUT/podcast_script.md" --settings "$AUDIO_OUT/audio_settings.json"
```

审核并按需编辑清单的 `spoken_text`。改写必须在 `pronunciation_notes` 记录该段完整的 `segment_id`、`original`（原 text）、`spoken`（改后 spoken_text）、`reason`。不改正文事实，不机械音译。完成核对后：

```bash
"$AUDIO_PY" "$AUDIO_SKILL/scripts/podcast_audio.py" --manifest "$AUDIO_OUT/audio_manifest.json" --review-text "已逐段核对原稿、角色、发音改写和停顿；依据见 quality_report.md"
```

此命令记录本次输入指纹与核对说明，并导出清洁稿；不是自动编辑评分。若 `require_script_gate` 为 true，配音前另在清单记录真实的 `script_status: PASSED` 和 `script_review_evidence`（审稿文件路径、对应稿件 SHA-256、结论），不能仅为启动脚本伪造通过。

先选有代表性的实际片段；下例编号仅演示，要根据本稿替换，并覆盖全部实际角色：

```bash
"$AUDIO_PY" "$AUDIO_SKILL/scripts/podcast_audio.py" --manifest "$AUDIO_OUT/audio_manifest.json" --synthesize --ids s0001,s0002
"$AUDIO_PY" "$AUDIO_SKILL/scripts/podcast_audio.py" --manifest "$AUDIO_OUT/audio_manifest.json" --assemble --ids s0001,s0002
"$AUDIO_PY" "$AUDIO_SKILL/scripts/verify_audio.py" --manifest "$AUDIO_OUT/audio_manifest.json" --sample --report "$AUDIO_OUT/sample_check.json"
```

`--synthesize` 自带在线声线预检，不需要重复单独 `--preflight`。通过样段技术核验后：

```bash
"$AUDIO_PY" "$AUDIO_SKILL/scripts/podcast_audio.py" --manifest "$AUDIO_OUT/audio_manifest.json" --synthesize
"$AUDIO_PY" "$AUDIO_SKILL/scripts/podcast_audio.py" --manifest "$AUDIO_OUT/audio_manifest.json" --assemble
"$AUDIO_PY" "$AUDIO_SKILL/scripts/verify_audio.py" --manifest "$AUDIO_OUT/audio_manifest.json" --report "$AUDIO_OUT/audio_independent_check.json"
```

生成器逐段保存原始 MP3 和标准 WAV，规范为 24 kHz、单声道、16 位 PCM，整集 MP3 为 96 kbps。时间轴以 PCM 样本数为准。独立验证器不导入生成器，校验文本/声音映射、哈希、完整解码、PCM 精确拼接、停顿、MP3 帧覆盖及约定时长。电平观察不能证明发音正确或听感合格。

## 失败、续作和最终状态

重复运行同一 `--synthesize` 只复用校验通过的缓存，补做剩余片段。每段累计最多三次，重启不归零；预检的连续失败尝试同样跨重启保留。单次服务请求超时 45 秒。服务不可用或预算耗尽时停止新增相关请求，记录具体错误；不循环重跑以绕过预算。

用户明确增加某片段额度后，才可向该片段的 `retry_authorizations` 追加 `status: APPROVED`、正整数 `additional_attempts`、用户授权原话和时间；不得复制其他任务的授权。预检三次失败后保留记录并报告阻塞，不自行清除历史。

源稿、设置文件和已生成的输入指纹不能就地修改。变更建立新目录；跨版本复用属于可选优化，仅复制相同声音输入与处理规格且重新校验通过的片段，不覆盖旧验收记录。封面独立生成/补做，不重合成音频。

独立验证脚本只写报告，不改清单。总执行者读报告后把 `audio_technical_status` 写回清单，整合 `quality_report.md`、`delivery.json`。音频全段齐全、适用文字门禁和技术校验通过、用户选择的封面已完成才写 `COMPLETE`；尚待封面选择或封面失败时写 `INCOMPLETE` 并说明已有完整音频。人工听审默认 `NOT_TESTED`。无需封面时 `cover_status: NOT_REQUESTED`、`cover_path: null`。

本地交付至少包含：保留的原稿、`podcast_script.md`、`podcast_script_tts.md`、`audio_settings.json`、`audio_manifest.json`、`audio.wav`、`audio.mp3`、`segments/`、`audio_independent_check.json`、`quality_report.md`、`delivery.json`；需要封面时加真实图片和 `cover_record.json`。已有研究和编辑材料按项目要求复制或引用，不为了普通 TTS 编造研究简报。

## 离线验证

```bash
"$AUDIO_PY" -m unittest discover -s "$AUDIO_SKILL/tests" -v
```

测试使用本地合成信号和服务桩；它们验证程序逻辑，不代表已经实际在线合成或人工听审。
