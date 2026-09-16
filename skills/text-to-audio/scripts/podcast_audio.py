#!/usr/bin/env python3
"""播客配音适配层：清单驱动、有限重试、可校验缓存和 PCM 拼接。"""
from __future__ import annotations
import argparse
import asyncio
import datetime as dt
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import wave

VERSION = "text-to-audio-1.0"
RATE = 24000
FORMAT = {"sample_rate_hz": RATE, "channels": 1, "pcm_bits": 16, "mp3_bitrate_kbps": 96}

def validate_settings(data):
    speakers = data.get("speakers")
    assert isinstance(speakers, dict) and speakers, "请先让用户选择音色，不能填默认声线"
    labels = []
    for key, speaker in speakers.items():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", key), "角色 ID 必须为小写英文、数字或下划线"
        label = speaker.get("label", "")
        assert label.strip() == label and label and not re.search(r"[:：\n\[\]#]", label), "角色名无效"
        assert re.fullmatch(r"[a-z]{2}-[A-Z]{2}-[A-Za-z0-9]+Neural", speaker.get("voice", "")), "缺少明确的声线 ID"
        assert re.fullmatch(r"[+-]\d+%", speaker.get("rate", "")), "语速参数格式错误"
        assert re.fullmatch(r"[+-]\d+Hz", speaker.get("pitch", "")), "音高参数格式错误"
        labels.append(label)
    assert len(set(labels)) == len(labels), "角色显示名称不能重复"
    assert isinstance(data.get("selection_basis"), str) and data["selection_basis"].strip(), "缺少用户选择依据"
    assert "cover" in data and (data["cover"] is None or type(data["cover"]) is bool), "封面选择必须为 true、false 或待选择的 null"
    assert type(data.get("require_script_gate")) is bool, "明确当前项目是否要求文字门禁"
    bounds = data.get("duration_range_seconds")
    assert bounds is None or (isinstance(bounds, list) and len(bounds) == 2 and all(type(n) in (int, float) for n in bounds) and 0 < bounds[0] <= bounds[1]), "时长范围无效"
    return data

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".stage-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=180).stdout

def versions():
    assert "libmp3lame" in run("ffmpeg", "-hide_banner", "-encoders"), "缺少 MP3 编码器"
    return {"edge_tts": importlib.metadata.version("edge-tts"),
            "python": __import__("sys").version.split()[0],
            "ffmpeg": run("ffmpeg", "-version").splitlines()[0],
            "ffprobe": run("ffprobe", "-version").splitlines()[0],
            "adapter": VERSION, "remote_revision": "unreported"}

def parse_script(path, speakers):
    content = Path(path).read_text()
    match = re.search(r"^## 正文\s*$\n(.*?)(?=^## |\Z)", content, re.M | re.S)
    if match is None: raise ValueError("请准备含 ## 正文 和逐段角色标签的稿件")
    body = match[1]
    labels = {s["label"]: key for key, s in speakers.items()}
    result, chapter = [], ""
    for line in body.splitlines():
        line = line.strip()
        if not line: continue
        if line.startswith("### "):
            chapter = line[4:]
            continue
        match = re.fullmatch(r"([^：]+)：(.+)", line)
        if not match or match[1] not in labels: raise ValueError(f"正文存在未映射的角色或非台词：{line[:60]}")
        speaker = labels[match[1]]
        chunks = re.split(r"(\[短停顿\]|\[停顿\]|\[停顿\s*\d+\s*毫秒\])", match[2])
        for chunk in chunks:
            if not chunk: continue
            if re.fullmatch(r"\[(?:短停顿|停顿(?:\s*\d+\s*毫秒)?)\]", chunk):
                assert result, "开头不能有孤立停顿"
                digits = re.search(r"\d+", chunk)
                result[-1]["explicit_pause_ms"] = int(digits[0]) if digits else (250 if chunk == "[短停顿]" else 800)
                continue
            notes = re.findall(r"\[[^\]]+\]", chunk)
            text = re.sub(r"\[[^\]]+\]", "", chunk).strip()
            if not text: continue
            result.append({"id": f"s{len(result)+1:04d}", "speaker": speaker, "text": text,
                           "spoken_text": text, "chapter": chapter, "editorial_notes": notes, "status": "PENDING"})
    assert result
    for index, item in enumerate(result):
        following = result[index + 1] if index + 1 < len(result) else None
        default = 0 if not following else (800 if following["chapter"] != item["chapter"] else (250 if following["speaker"] != item["speaker"] else 0))
        item["pause_after_ms"] = item.get("explicit_pause_ms", default)
    return result

def input_identity(data):
    return {"source_script": data["source_script"], "settings_file": data["settings_file"], "speakers": data["speakers"], "audio_format": data["audio_format"],
            "segments": [{k: s[k] for k in ("id", "speaker", "text", "spoken_text", "pause_after_ms")} for s in data["segments"]]}

def segment_key(data, segment):
    return fingerprint({"spoken_text": segment["spoken_text"], "voice": data["speakers"][segment["speaker"]],
                        "client": data["tool_versions"]["edge_tts"], "processing": FORMAT, "adapter": VERSION})

def prepare(script, manifest, settings_path):
    assert not manifest.exists(), "已有清单请续作；输入改变须新版本目录"
    assert settings_path is not None, "缺少 --settings；请先收集用户的音色选择"
    assert script.parent == manifest.parent and settings_path.parent == manifest.parent, "请先把正式稿和设置副本保存到清单目录"
    settings = validate_settings(json.loads(settings_path.read_text()))
    data = {"schema_version": 1, "mode": "audio", "created_at": now(),
            "source_script": {"path": script.name, "sha256": sha(script)}, "tts_engine": "edge-tts",
            "settings_file": {"path": settings_path.name, "sha256": sha(settings_path)},
            "speakers": settings["speakers"], "pronunciation_notes": [], "segments": parse_script(script, settings["speakers"]), "audio_format": FORMAT,
            "duration_range_seconds": settings.get("duration_range_seconds"), "require_script_gate": settings["require_script_gate"],
            "text_preparation_status": "NOT_TESTED", "cover_requested": settings["cover"],
            "tool_versions": versions(), "generation_runs": [], "status": "NOT_STARTED", "script_status": "NOT_TESTED",
            "audio_technical_status": "NOT_TESTED", "human_listening_status": "NOT_TESTED"}
    write_json(manifest, data)
    export_tts(manifest, data)
    return data

def export_tts(manifest, data):
    lines = ["# 清洁朗读稿", "", "以下台词与 audio_manifest.json 的 spoken_text 顺序一致。角色标签不朗读。", ""]
    for s in data["segments"]:
        lines.extend([data["speakers"][s["speaker"]]["label"] + "：" + s["spoken_text"], ""])
    manifest.with_name("podcast_script_tts.md").write_text("\n".join(lines))

def validate_input(root, data):
    settings_path = root / data["settings_file"]["path"]
    assert sha(settings_path) == data["settings_file"]["sha256"], "用户选择文件已改变；请建立新版本目录"
    settings = validate_settings(json.loads(settings_path.read_text()))
    assert data["speakers"] == settings["speakers"], "声音映射与用户选择不符"
    assert data["require_script_gate"] == settings["require_script_gate"]
    assert data["duration_range_seconds"] == settings.get("duration_range_seconds")
    assert sha(root / data["source_script"]["path"]) == data["source_script"]["sha256"], "源稿哈希不符"
    expected = parse_script(root / data["source_script"]["path"], data["speakers"])
    actual = data["segments"]
    assert [(s["id"], s["speaker"], s["text"], s["pause_after_ms"]) for s in expected] == [(s["id"], s["speaker"], s["text"], s["pause_after_ms"]) for s in actual], "稿件、角色、顺序或停顿不一致"
    assert data["audio_format"] == FORMAT
    for s in actual:
        assert s["speaker"] in data["speakers"] and s["spoken_text"].strip()
        assert not re.search(r"https?://|\[[^\]]*\]|^#|\|", s["spoken_text"]), "朗读文本含制作元素"
        assert not any(s["spoken_text"].startswith(v["label"] + "：") for v in data["speakers"].values()), "角色标签不得朗读"
        if s["spoken_text"] != s["text"]:
            assert any(n.get("segment_id") == s["id"] and n.get("original") == s["text"] and n.get("spoken") == s["spoken_text"] for n in data["pronunciation_notes"]), "每处朗读改写必须有逐段对照"
    lock_path = root / "input_fingerprint.json"
    identity = fingerprint(input_identity(data))
    if lock_path.exists():
        locked = json.loads(lock_path.read_text())
        assert locked["sha256"] == identity, "已生成输入发生改变；请建立新版本目录"
    return identity

def review_text(manifest, data, note):
    identity = validate_input(manifest.parent, data)
    assert note.strip(), "请记录实际完成的文本核对及证据位置"
    data["text_review"] = {"checked_at": now(), "input_fingerprint": identity, "note": note}
    data["text_preparation_status"] = "PASSED"
    export_tts(manifest, data)
    write_json(manifest, data)

def inspect(path, kind):
    probe = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)))
    streams = probe["streams"]
    assert len(streams) == 1 and streams[0]["codec_type"] == "audio"
    stream = streams[0]
    run("ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-")
    result = {"sha256": sha(path), "bytes": path.stat().st_size, "codec": stream["codec_name"],
              "sample_rate_hz": int(stream["sample_rate"]), "channels": stream["channels"],
              "duration_seconds": float(probe["format"]["duration"]), "bit_rate": int(stream.get("bit_rate", 0))}
    assert result["duration_seconds"] > 0
    if kind == "wav":
        with wave.open(str(path)) as source:
            assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, RATE)
            result["frames"] = source.getnframes()
            pcm = source.readframes(result["frames"])
            assert len(pcm) == result["frames"] * 2 and any(pcm), "空白或截断 PCM"
            result["duration_seconds"] = result["frames"] / RATE
        assert result["codec"] == "pcm_s16le"
    if kind == "mp3":
        assert (result["codec"], result["sample_rate_hz"], result["channels"], result["bit_rate"]) == ("mp3", RATE, 1, 96000)
    return result

def check_record(root, data, segment):
    assert segment["status"] == "PASSED"
    assert segment["input_fingerprint"] == segment_key(data, segment)
    for key, kind in (("raw_audio", "raw"), ("wav_audio", "wav")):
        expected = segment[key]
        current = inspect(root / expected["path"], kind)
        for field in ("sha256", "bytes", "codec", "sample_rate_hz", "channels", "duration_seconds"):
            assert current[field] == expected[field], f"{segment['id']} {field} 不符"
    return True

def recover_record(root, data, segment):
    """恢复音频已原子落盘、主清单尚未登记时留下的段收据。"""
    receipt = root / "segments" / segment["id"] / "segment.json"
    if not receipt.exists(): return False
    candidate = json.loads(receipt.read_text())
    for key in ("id", "speaker", "text", "spoken_text", "pause_after_ms"):
        assert candidate[key] == segment[key], f"断点收据 {key} 与本次输入不符"
    check_record(root, data, candidate)
    for key in ("raw_audio", "wav_audio", "input_fingerprint", "status"):
        segment[key] = candidate[key]
    segment["attempts"] = max(segment.get("attempts", 0), candidate.get("attempts", 0))
    segment.pop("error", None)
    segment.setdefault("recovered_receipts", []).append({"checked_at": now(), "path": str(receipt.relative_to(root)), "sha256": sha(receipt)})
    return True

async def bounded(action, records, previous=0, checkpoint=lambda: None, max_attempts=3):
    for attempt in range(previous + 1, max_attempts + 1):
        rec = {"attempt": attempt, "started_at": now(), "status": "RUNNING"}
        records.append(rec)
        checkpoint()
        try:
            value = await asyncio.wait_for(action(), timeout=45)
            rec.update(status="PASSED", finished_at=now())
            checkpoint()
            return value
        except Exception as exc:
            rec.update(status="FAILED", error=f"{type(exc).__name__}: {str(exc)[:500]}", finished_at=now())
            checkpoint()
            if attempt == max_attempts: raise
            await asyncio.sleep(attempt)
    raise RuntimeError(f"本片段已用完授权的 {max_attempts} 次尝试；需要用户明确增加额度才能继续")

async def preflight(data, records, checkpoint=lambda: None):
    import edge_tts
    observed_versions = versions()
    for key in ("edge_tts", "adapter"):
        assert observed_versions[key] == data["tool_versions"][key], "生成工具版本已改变；请建立新版本目录，不能覆写旧版本记录"
    prior = 0
    for attempt in reversed(data.setdefault("voice_list_attempts", [])):
        if attempt["status"] == "PASSED": break
        prior += 1
    async def action():
        voices = await edge_tts.list_voices()
        names = {v["ShortName"] for v in voices}
        wanted = {s["voice"] for s in data["speakers"].values()}
        assert wanted <= names, f"声线不可用：{wanted - names}"
        return voices
    def save():
        data["voice_list_attempts"] = history + records
        checkpoint()
    history = list(data["voice_list_attempts"])
    voices = await bounded(action, records, previous=prior, checkpoint=save)
    wanted = {s["voice"] for s in data["speakers"].values()}
    return {"checked_at": now(), "voices": [v for v in voices if v["ShortName"] in wanted], "observed_tool_versions": observed_versions, "status": "PASSED"}

async def synthesize(manifest, data, ids):
    import edge_tts
    root = manifest.parent
    identity = validate_input(root, data)
    assert data["text_preparation_status"] == "PASSED" and data["text_review"]["input_fingerprint"] == identity, "当前版本朗读文本尚未核对"
    if data["require_script_gate"]:
        assert data["script_status"] == "PASSED" and data.get("script_review_evidence"), "项目文字门禁尚未通过或缺少证据"
    assert versions()["edge_tts"] == data["tool_versions"]["edge_tts"], "客户端版本已改变；请建立新版本目录"
    selected = [s for s in data["segments"] if not ids or s["id"] in ids]
    if ids: assert set(ids) == {s["id"] for s in selected}
    pending = []
    for segment in selected:
        if segment["status"] != "PASSED":
            try:
                recover_record(root, data, segment)
            except Exception as exc:
                segment.setdefault("invalid_cache", []).append({"time": now(), "reason": f"断点收据无效：{exc}"})
                location = root / "segments" / segment["id"]
                if location.exists():
                    os.replace(location, location.with_name(segment["id"] + ".invalid-" + dt.datetime.now().strftime("%H%M%S%f")))
        if segment["status"] == "PASSED":
            try:
                check_record(root, data, segment)
                continue
            except Exception as exc:
                segment.setdefault("invalid_cache", []).append({"time": now(), "reason": str(exc)})
                location = root / "segments" / segment["id"]
                if location.exists():
                    os.replace(location, location.with_name(segment["id"] + ".invalid-" + dt.datetime.now().strftime("%H%M%S%f")))
                segment["status"] = "PENDING"
        pending.append(segment)
    write_json(manifest, data)
    if not pending:
        print("全部选中片段通过缓存校验；未做新在线验证", flush=True)
        return
    record = {"started_at": now(), "selected_ids": [s["id"] for s in pending], "preflight_attempts": []}
    data["generation_runs"].append(record)
    data["status"] = "RUNNING"
    data["audio_technical_status"] = "NOT_TESTED"
    write_json(manifest, data)
    try:
        record["preflight"] = await preflight(data, record["preflight_attempts"], lambda: write_json(manifest, data))
    except Exception as exc:
        record.update(status="FAILED", error=f"预检失败：{exc}", finished_at=now())
        data["status"] = "INCOMPLETE"
        write_json(manifest, data)
        raise
    write_json(root / "input_fingerprint.json", {"sha256": identity, "locked_at": now()})
    write_json(manifest, data)
    for segment in pending:
        previous_attempts = sum(len(h["attempts"]) for h in segment.get("generation_history", []))
        attempt_limit = 3 + sum(a["additional_attempts"] for a in segment.get("retry_authorizations", []) if a["status"] == "APPROVED")
        attempts = []
        segment.setdefault("generation_history", []).append({"run_started_at": record["started_at"], "attempts": attempts})
        target = root / "segments" / segment["id"]
        target.parent.mkdir(exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=".stage-", dir=target.parent) as tmp:
                stage = Path(tmp)
                config = data["speakers"][segment["speaker"]]
                async def action():
                    await edge_tts.Communicate(segment["spoken_text"], config["voice"], rate=config["rate"], pitch=config["pitch"]).save(str(stage / "raw.mp3"))
                await bounded(action, attempts, previous=previous_attempts, checkpoint=lambda: write_json(manifest, data), max_attempts=attempt_limit)
                raw = inspect(stage / "raw.mp3", "raw")
                run("ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(stage / "raw.mp3"), "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(stage / "audio.wav"))
                wav = inspect(stage / "audio.wav", "wav")
                raw["path"] = str((target / "raw.mp3").relative_to(root))
                wav["path"] = str((target / "audio.wav").relative_to(root))
                segment.update(input_fingerprint=segment_key(data, segment), attempts=previous_attempts + len(attempts), raw_audio=raw, wav_audio=wav, status="PASSED")
                segment.pop("error", None)
                write_json(stage / "segment.json", segment)
                if target.exists():
                    os.replace(target, target.with_name(segment["id"] + ".previous-" + dt.datetime.now().strftime("%H%M%S%f")))
                os.replace(stage, target)
            print(f"{segment['id']} PASSED {wav['duration_seconds']:.2f}s", flush=True)
        except Exception as exc:
            segment.update(status="FAILED", attempts=previous_attempts + len(attempts), error=f"{type(exc).__name__}: {exc}")
            print(f"{segment['id']} FAILED {exc}", flush=True)
        write_json(manifest, data)
    record.update(finished_at=now(), status="FAILED" if any(s["status"] != "PASSED" for s in selected) else "PASSED")
    data["status"] = "INCOMPLETE" if any(s["status"] == "FAILED" for s in data["segments"]) else "RUNNING"
    write_json(manifest, data)

def assemble(manifest, data, ids=None):
    root = manifest.parent
    validate_input(root, data)
    selected = [s for s in data["segments"] if not ids or s["id"] in ids]
    assert selected
    if ids: assert set(ids) == {s["id"] for s in selected}, "样段包含未知编号"
    for segment in selected: check_record(root, data, segment)
    if not ids:
        data["audio_technical_status"] = "NOT_TESTED"
        data["status"] = "RUNNING"
        write_json(manifest, data)
    prefix = "sample" if ids else "audio"
    cursor, timeline = 0, []
    with tempfile.TemporaryDirectory(prefix=".mix-", dir=root) as tmp:
        stage = Path(tmp)
        with wave.open(str(stage / f"{prefix}.wav"), "wb") as out:
            out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            for index, segment in enumerate(selected):
                with wave.open(str(root / segment["wav_audio"]["path"]), "rb") as source:
                    count = source.getnframes()
                    out.writeframes(source.readframes(count))
                pause = segment["pause_after_ms"] if index + 1 < len(selected) or not ids else segment.get("explicit_pause_ms", 0)
                gap = round(pause * RATE / 1000)
                timeline.append({"id": segment["id"], "start_frame": cursor, "end_frame": cursor + count, "pause_frames": gap,
                                 "start_seconds": cursor / RATE, "end_seconds": (cursor + count) / RATE})
                out.writeframes(b"\0\0" * gap)
                cursor += count + gap
        run("ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(stage / f"{prefix}.wav"), "-c:a", "libmp3lame", "-b:a", "96k", "-ar", str(RATE), "-ac", "1", str(stage / f"{prefix}.mp3"))
        outputs = {}
        for extension in ("wav", "mp3"):
            name = f"{prefix}.{extension}"
            outputs[extension] = {"path": name, **inspect(stage / name, extension)}
            os.replace(stage / name, root / name)
    receipt = {"created_at": now(), "files": outputs, "timeline": timeline,
               "assembly_fingerprint": fingerprint({"segments": [(s["input_fingerprint"], t["pause_frames"]) for s, t in zip(selected, timeline)], "format": FORMAT}),
               "frames": cursor, "actual_duration_seconds": cursor / RATE, "processing": "仅规格转换与插入静音；无增益、响度处理、裁剪或变速"}
    if ids:
        data["representative_sample"] = receipt
    else:
        for segment, timing in zip(selected, timeline): segment["timeline"] = timing
        data["assembled_audio"] = receipt
        data["actual_duration_seconds"] = cursor / RATE
        bounds = data.get("duration_range_seconds")
        data["duration_in_range"] = bounds[0] <= cursor / RATE <= bounds[1] if bounds else None
    write_json(manifest, data)
    print(f"{prefix}: {cursor / RATE:.3f}s", flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--prepare", type=Path, help="从正式稿创建新清单，已有文件不覆盖")
    parser.add_argument("--settings", type=Path, help="准备时必填：保存实际音色和封面选择的 JSON")
    parser.add_argument("--review-text", metavar="NOTE", help="完成朗读稿核对后登记依据；不代表独立编辑审稿")
    parser.add_argument("--export-tts", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--synthesize", action="store_true")
    parser.add_argument("--assemble", action="store_true")
    parser.add_argument("--ids", help="代表性样段编号，以逗号分隔")
    args = parser.parse_args()
    path = args.manifest.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".audio.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = prepare(args.prepare.resolve(), path, args.settings.resolve() if args.settings else None) if args.prepare else json.loads(path.read_text())
        if args.review_text: review_text(path, data, args.review_text)
        if args.export_tts: export_tts(path, data)
        if args.preflight:
            attempts = []
            try:
                result = asyncio.run(preflight(data, attempts, lambda: write_json(path, data)))
            except Exception as exc:
                result = {"status": "FAILED", "error": str(exc)}
            write_json(path.parent / "preflight.json", {"result": result, "attempts": attempts, "tool_versions": data["tool_versions"]})
            print(json.dumps(result, ensure_ascii=False))
            if result["status"] != "PASSED": raise RuntimeError(result["error"])
        ids = args.ids.split(",") if args.ids else None
        if args.synthesize:
            asyncio.run(synthesize(path, data, ids))
            assert not any(s["status"] == "FAILED" for s in data["segments"] if not ids or s["id"] in ids), "部分片段失败，请查看清单"
        if args.assemble: assemble(path, data, ids)

if __name__ == "__main__": main()
