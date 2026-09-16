#!/usr/bin/env python3
"""独立只读核验播客清单与真实音频，仅写入指定的 JSON 报告。"""
from __future__ import annotations

import argparse
from array import array
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import wave

RATE = 24000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def json_digest(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


def command(*args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    require(result.returncode == 0, f"命令失败 {args[0]}: {result.stderr.decode(errors='replace')[-1500:]}")
    return result.stdout


def script_segments(text, speakers):
    """直接从章节与台词恢复顺序；不调用或导入生成器。"""
    match = re.search(r"^## 正文\s*$\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    require(match is not None, "正式稿缺少正文边界")
    items, chapter = [], ""
    labels = {speaker["label"]: role for role, speaker in speakers.items()}
    for line in match[1].splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("### "):
            chapter = line[4:]
            continue
        speaker_match = re.fullmatch(r"([^：]+)：(.+)", line)
        require(speaker_match is not None and speaker_match[1] in labels, f"未识别正文: {line[:60]}")
        role = labels[speaker_match[1]]
        for part in re.split(r"(\[短停顿\]|\[停顿\]|\[停顿\s*\d+\s*毫秒\])", speaker_match[2]):
            if not part.strip():
                continue
            if re.fullmatch(r"\[(?:短停顿|停顿(?:\s*\d+\s*毫秒)?)\]", part):
                require(bool(items), "停顿无前置台词")
                number = re.search(r"\d+", part)
                items[-1]["explicit"] = int(number[0]) if number else (250 if part == "[短停顿]" else 800)
            else:
                clean = re.sub(r"\[[^\]]*\]", "", part).strip()
                if clean:
                    items.append({"speaker": role, "text": clean, "chapter": chapter})
    for index, item in enumerate(items):
        next_item = items[index + 1] if index + 1 < len(items) else None
        default = 0
        if next_item:
            default = 800 if next_item["chapter"] != item["chapter"] else (250 if next_item["speaker"] != item["speaker"] else 0)
        item["pause"] = item.get("explicit", default)
    return items


def inspect_file(path, record, kind):
    raw_bytes = path.read_bytes()
    require(digest(raw_bytes) == record["sha256"], f"哈希不符: {path}")
    require(len(raw_bytes) == record["bytes"], f"字节数不符: {path}")
    probe = json.loads(command("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)))
    require(len(probe["streams"]) == 1, f"音轨数量错误: {path}")
    stream = probe["streams"][0]
    require(stream["codec_type"] == "audio", f"非音频流: {path}")
    observed = {"codec": stream["codec_name"], "sample_rate_hz": int(stream["sample_rate"]), "channels": stream["channels"]}
    for key, value in observed.items():
        require(record[key] == value, f"收据 {key} 错误: {path}")
    # 输出完整 PCM，-xerror 将解码错误视为失败；未读取任何 status。
    decoded = command("ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0", "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", "-f", "s16le", "-")
    require(len(decoded) > 0 and len(decoded) % 2 == 0 and any(decoded), f"音频为空或截断: {path}")
    duration = float(probe["format"]["duration"])
    if kind == "wav":
        require(observed == {"codec": "pcm_s16le", "sample_rate_hz": RATE, "channels": 1}, f"WAV 格式错误: {path}")
        with wave.open(str(path), "rb") as handle:
            require(handle.getsampwidth() == 2 and handle.getcomptype() == "NONE", "WAV 位深/压缩错误")
            count = handle.getnframes()
            payload = handle.readframes(count)
        require(payload == decoded and len(payload) == count * 2, f"WAV 样本/解码不一致: {path}")
        require(record["frames"] == count, f"WAV 帧数收据错误: {path}")
        duration = count / RATE
    elif kind == "mp3":
        require(observed == {"codec": "mp3", "sample_rate_hz": RATE, "channels": 1}, f"整集 MP3 格式错误: {path}")
        require(int(stream["bit_rate"]) == 96000 and record["bit_rate"] == 96000, "MP3 非 96 kbps")
    require(abs(duration - record["duration_seconds"]) < 1e-6, f"时长收据错误: {path}")
    return decoded, {"path": str(path), **observed, "sha256": digest(raw_bytes), "decoded_frames": len(decoded) // 2, "duration_seconds": duration, "complete_decode": True}


def levels(pcm):
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    rms = math.sqrt(sum(v * v for v in samples) / len(samples))
    peak = max(abs(v) for v in samples)
    first = next((i for i, value in enumerate(samples) if value), None)
    last = next((len(samples) - 1 - i for i, value in enumerate(reversed(samples)) if value), None)
    return {"rms_dbfs": 20 * math.log10(rms / 32768) if rms else None,
            "peak_dbfs": 20 * math.log10(peak / 32768) if peak else None,
            "full_scale_samples": sum(abs(v) >= 32767 for v in samples),
            "first_nonzero_frame": first,
            "last_nonzero_frame": last}


def verify(args, report):
    manifest = args.manifest.resolve()
    root = manifest.parent
    data = json.loads(manifest.read_text())
    report["manifest_sha256"] = digest(manifest.read_bytes())
    require(data["audio_format"] == {"sample_rate_hz": RATE, "channels": 1, "pcm_bits": 16, "mp3_bitrate_kbps": 96}, "清单格式不符合项目规格")
    settings_path = root / data["settings_file"]["path"]
    require(digest(settings_path.read_bytes()) == data["settings_file"]["sha256"], "用户音色选择文件哈希错误")
    settings = json.loads(settings_path.read_text())
    require(data["speakers"] == settings["speakers"], "清单与用户选择的声线/参数不符")
    require(data["duration_range_seconds"] == settings.get("duration_range_seconds"), "目标时长与设置不符")
    source = root / data["source_script"]["path"]
    require(digest(source.read_bytes()) == data["source_script"]["sha256"], "正式稿 SHA-256 不符")
    expected = script_segments(source.read_text(), settings["speakers"])
    segments = data["segments"]
    require(len(expected) == len(segments) > 0, "正式稿与清单片段数量不同")
    identity = json_digest({"source_script": data["source_script"], "settings_file": data["settings_file"],
                            "speakers": data["speakers"], "audio_format": data["audio_format"],
                            "segments": [{k: s[k] for k in ("id", "speaker", "text", "spoken_text", "pause_after_ms")} for s in segments]})
    require(data.get("text_review", {}).get("input_fingerprint") == identity, "当前输入与已核对朗读稿不符")
    locked = json.loads((root / "input_fingerprint.json").read_text())
    require(locked["sha256"] == identity, "当前输入与开始合成时锁定的输入不同")
    ids = [s["id"] for s in segments]
    require(len(set(ids)) == len(ids), "片段 ID 重复")
    for reference, segment in zip(expected, segments):
        require((reference["speaker"], reference["text"], reference["pause"]) == (segment["speaker"], segment["text"], segment["pause_after_ms"]), f"正文/角色/停顿不符: {segment['id']}")
        spoken = segment["spoken_text"]
        require(bool(spoken.strip()) and not re.search(r"https?://|\[[^\]]*\]|^#|\|", spoken), f"朗读文本含制作标记: {segment['id']}")
        require(not any(spoken.startswith(s["label"] + "：") for s in data["speakers"].values()), "朗读文本包含角色标签")
        if spoken != segment["text"]:
            require(any(n.get("segment_id") == segment["id"] and n.get("original") == segment["text"] and n.get("spoken") == spoken for n in data.get("pronunciation_notes", [])), "朗读改写缺少逐段对照记录")
    tts_path = root / "podcast_script_tts.md"
    tts_lines = re.findall(r"^([^：\n]+)：(.+)$", tts_path.read_text(), re.M)
    require(tts_lines == [(data["speakers"][s["speaker"]]["label"], s["spoken_text"]) for s in segments], "清洁稿与清单朗读内容/顺序不符")
    report["text_checks"] = {"source_sha256": digest(source.read_bytes()), "tts_sha256": digest(tts_path.read_bytes()), "segments": len(segments), "passed": True}
    receipt = data["representative_sample" if args.sample else "assembled_audio"]
    timeline = receipt["timeline"]
    selected_ids = [t["id"] for t in timeline]
    require(len(selected_ids) == len(set(selected_ids)), "时间轴编号重复")
    selected = [s for s in segments if s["id"] in selected_ids] if args.sample else segments
    require(selected_ids == [s["id"] for s in selected], "时间轴缺项、未知项或顺序错误")
    if args.sample:
        require({s["speaker"] for s in selected} == {s["speaker"] for s in segments}, "代表性样段未覆盖全部实际角色")
    expected_pcm = bytearray()
    observations = []
    for index, (segment, timing) in enumerate(zip(selected, timeline)):
        key = json_digest({"spoken_text": segment["spoken_text"], "voice": data["speakers"][segment["speaker"]],
                           "client": data["tool_versions"]["edge_tts"], "processing": data["audio_format"], "adapter": data["tool_versions"]["adapter"]})
        require(segment.get("input_fingerprint") == key, f"音频缓存不属于当前朗读文本/声线/版本: {segment['id']}")
        raw_pcm, raw_info = inspect_file(root / segment["raw_audio"]["path"], segment["raw_audio"], "raw")
        pcm, wav_info = inspect_file(root / segment["wav_audio"]["path"], segment["wav_audio"], "wav")
        require(raw_pcm == pcm, f"原始音频到标准 PCM 不一致: {segment['id']}")
        start = len(expected_pcm) // 2
        end = start + len(pcm) // 2
        pause_ms = segment["pause_after_ms"]
        if args.sample and index == len(selected) - 1:
            pause_ms = segment.get("explicit_pause_ms", 0)
        pause_frames = round(pause_ms * RATE / 1000)
        correct = {"id": segment["id"], "start_frame": start, "end_frame": end, "pause_frames": pause_frames, "start_seconds": start / RATE, "end_seconds": end / RATE}
        require(timing == correct, f"时间轴不符: {segment['id']}")
        if not args.sample:
            require(segment["timeline"] == correct, f"片段时间轴不符: {segment['id']}")
        expected_pcm.extend(pcm)
        expected_pcm.extend(b"\0\0" * pause_frames)
        observation = {"id": segment["id"], "speaker": segment["speaker"], "raw": raw_info, "wav": wav_info, "levels": levels(pcm)}
        observations.append(observation)
    report["segments"] = observations
    assembly_key = json_digest({"segments": [(s["input_fingerprint"], t["pause_frames"]) for s, t in zip(selected, timeline)], "format": data["audio_format"]})
    require(receipt["assembly_fingerprint"] == assembly_key, "拼接输入指纹不一致")
    wav_pcm, wav_info = inspect_file(root / receipt["files"]["wav"]["path"], receipt["files"]["wav"], "wav")
    mp3_pcm, mp3_info = inspect_file(root / receipt["files"]["mp3"]["path"], receipt["files"]["mp3"], "mp3")
    require(wav_pcm == expected_pcm, "整集 PCM 与逐段拼接/精确静音不一致")
    frames = len(wav_pcm) // 2
    require(frames == receipt["frames"] and frames / RATE == receipt["actual_duration_seconds"], "整集帧数或实测时长不符")
    require(len(mp3_pcm) == len(wav_pcm), "整集 MP3 解码覆盖帧数与 WAV 不同")
    # MP3 为有损格式：不做错误的逐字节相等要求，以完整覆盖及失真观察核查编码。
    reference, compressed = array("h"), array("h")
    reference.frombytes(wav_pcm)
    compressed.frombytes(mp3_pcm)
    if sys.byteorder != "little":
        reference.byteswap()
        compressed.byteswap()
    energy = sum(v * v for v in reference)
    error = sum((a - b) ** 2 for a, b in zip(reference, compressed))
    snr = 10 * math.log10(energy / error) if error else None
    require(snr is None or snr >= 15, f"MP3 与 WAV 严重偏离: SNR={snr}")
    if not args.sample:
        bounds = settings.get("duration_range_seconds")
        if bounds:
            require(bounds[0] <= frames / RATE <= bounds[1], "完整音频不在约定时长范围")
        require(data["actual_duration_seconds"] == frames / RATE, "顶层时长不符")
    transitions = []
    for before, after in zip(observations, observations[1:]):
        if before["speaker"] != after["speaker"]:
            difference = abs(before["levels"]["rms_dbfs"] - after["levels"]["rms_dbfs"])
            transitions.append({"from": before["id"], "to": after["id"], "rms_difference_db": difference})
            if difference > 8:
                report["warnings"].append(f"{before['id']}→{after['id']} 整段 RMS 差 {difference:.2f} dB，建议试听核查；内容/停顿也会影响 RMS")
    report["audio"] = {"wav": wav_info, "mp3": mp3_info, "actual_duration_seconds": frames / RATE,
                       "pcm_exact_concatenation": True, "exact_silence": True, "head_tail_coverage": True,
                       "mp3_snr_db": snr, "levels": levels(wav_pcm), "speaker_transitions": transitions}
    report["audio_technical_status"] = "PASSED"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="实际音频清单")
    parser.add_argument("--report", type=Path, required=True, help="独立核验 JSON 报告输出")
    parser.add_argument("--sample", action="store_true", help="核验代表性样段，不要求整集时长")
    args = parser.parse_args()
    require(args.report.resolve() != args.manifest.resolve(), "报告不能覆盖清单")
    report = {"verifier": "independent-text-to-audio-v1", "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "scope": "sample" if args.sample else "full", "audio_technical_status": "FAILED", "human_listening_status": "NOT_TESTED", "errors": [], "warnings": [],
              "limitations": "技术检查不证明发音、声线身份或听感；台词内容仍需人工听审确认。未依赖生成器 status 或函数。"}
    try:
        verify(args, report)
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["audio_technical_status"], "report": str(args.report), "errors": report["errors"]}, ensure_ascii=False))
    return 0 if report["audio_technical_status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
