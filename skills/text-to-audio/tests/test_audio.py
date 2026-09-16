"""离线契约测试；不调用在线服务，不作为真实配音验收。"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
import shutil
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import podcast_audio as p
import verify_audio as v

DUAL = {"host": {"label": "女主持", "voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "pitch": "+0Hz"},
        "interviewer": {"label": "男主持", "voice": "zh-CN-YunyangNeural", "rate": "+0%", "pitch": "+0Hz"}}
SOLO = {"reader": {"label": "朗读者", "voice": "zh-CN-YunyangNeural", "rate": "+0%", "pitch": "+0Hz"}}

class ContractTests(unittest.TestCase):
    def test_inline_pauses_and_chapter_precedence(self):
        with tempfile.TemporaryDirectory() as name:
            file = Path(name) / 'script.md'
            file.write_text('## 正文\n### 甲\n女主持：第一句。[短停顿]第二句。\n女主持：第三句。\n### 乙\n男主持：第四句。[停顿]\n## 本集核心观点回顾\n')
            segments = p.parse_script(file, DUAL)
            self.assertEqual([s['pause_after_ms'] for s in segments], [250, 0, 800, 800])
            self.assertEqual([s['text'] for s in segments], ['第一句。', '第二句。', '第三句。', '第四句。'])

    def test_no_default_final_gap(self):
        with tempfile.TemporaryDirectory() as name:
            file = Path(name) / 'script.md'
            file.write_text('## 正文\n女主持：第一句。\n男主持：第二句。\n## 本集核心观点回顾\n')
            self.assertEqual([s['pause_after_ms'] for s in p.parse_script(file, DUAL)], [250, 0])

    def test_retry_budget_survives_continuation_and_records_before_request(self):
        records, snapshots, calls = [], [], []
        def checkpoint(): snapshots.append(json.loads(json.dumps(records)))
        async def failure():
            calls.append(True)
            self.assertEqual(snapshots[-1][-1]['status'], 'RUNNING')
            raise RuntimeError('测试服务失败')
        with self.assertRaises(RuntimeError):
            asyncio.run(p.bounded(failure, records, previous=2, checkpoint=checkpoint))
        self.assertEqual(len(calls), 1)
        self.assertEqual(records[0]['attempt'], 3)
        self.assertEqual(records[0]['status'], 'FAILED')
        with self.assertRaises(RuntimeError):
            asyncio.run(p.bounded(failure, [], previous=3))
        self.assertEqual(len(calls), 1)

    def test_role_notes_removed_and_unknown_lines_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            file = Path(name) / 'script.md'
            file.write_text('## 正文\n女主持：[语气放慢]这里是内容。\n## 本集核心观点回顾\n')
            self.assertEqual(p.parse_script(file, DUAL)[0]['spoken_text'], '这里是内容。')
            file.write_text('## 正文\n未标角色的文字\n## 本集核心观点回顾\n')
            with self.assertRaises(ValueError): p.parse_script(file, DUAL)

    def test_explicit_extended_budget_remains_bounded(self):
        calls, records = [], []
        async def failure():
            calls.append(True)
            raise RuntimeError('测试服务失败')
        with self.assertRaises(RuntimeError):
            asyncio.run(p.bounded(failure, records, previous=5, max_attempts=6))
        self.assertEqual(len(calls), 1)
        self.assertEqual(records[0]['attempt'], 6)
        with self.assertRaises(RuntimeError):
            asyncio.run(p.bounded(failure, [], previous=6, max_attempts=6))
        self.assertEqual(len(calls), 1)

class SkillTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.script = self.root / "podcast_script.md"
        self.manifest = self.root / "audio_manifest.json"
        self.settings = self.root / "audio_settings.json"

    def prepare(self, speakers=SOLO, body="朗读者：第一句。[短停顿]第二句。", duration=None):
        self.script.write_text("# 测试稿\n## 正文\n" + body + "\n## 参考资料\n正文外的资料不朗读。\n")
        p.write_json(self.settings, {"speakers": speakers, "selection_basis": "离线测试选择，不是实际用户音频任务", "cover": False,
                                     "require_script_gate": False, "duration_range_seconds": duration})
        data = p.prepare(self.script, self.manifest, self.settings)
        p.review_text(self.manifest, data, "离线测试内容核对")
        return data

    def make_local_audio(self, data):
        """用已标记的本地测试信号验证解码拼接，不把它当作真实配音。"""
        p.write_json(self.root / "input_fingerprint.json", {"sha256": p.fingerprint(p.input_identity(data))})
        for index, segment in enumerate(data["segments"]):
            target = self.root / "segments" / segment["id"]
            target.mkdir(parents=True)
            raw = target / "raw.mp3"
            wav = target / "audio.wav"
            p.run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={440 + 100 * index}:duration=0.4:sample_rate=24000", "-c:a", "libmp3lame", "-b:a", "96k", str(raw))
            p.run("ffmpeg", "-v", "error", "-i", str(raw), "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(wav))
            segment.update(status="PASSED", input_fingerprint=p.segment_key(data, segment),
                           raw_audio={"path": str(raw.relative_to(self.root)), **p.inspect(raw, "raw")},
                           wav_audio={"path": str(wav.relative_to(self.root)), **p.inspect(wav, "wav")})
        p.write_json(self.manifest, data)

    def verify(self, sample=False):
        report = {"warnings": []}
        v.verify(SimpleNamespace(manifest=self.manifest, sample=sample), report)
        return report

    def test_voice_selection_required_and_no_implicit_defaults(self):
        with self.assertRaisesRegex(AssertionError, "settings"):
            p.prepare(self.script, self.manifest, None)
        with self.assertRaisesRegex(AssertionError, "选择音色"):
            p.validate_settings({"speakers": {}})
        self.assertFalse(self.manifest.exists())

    def test_solo_keeps_selected_voice_and_excludes_reference_section(self):
        data = self.prepare()
        self.assertEqual(data["speakers"]["reader"]["voice"], "zh-CN-YunyangNeural")
        self.assertEqual([s["text"] for s in data["segments"]], ["第一句。", "第二句。"])
        self.assertEqual([s["pause_after_ms"] for s in data["segments"]], [250, 0])
        self.assertEqual([s["speaker"] for s in data["segments"]], ["reader", "reader"])
        self.assertEqual(data["script_status"], "NOT_TESTED")
        self.assertEqual(data["human_listening_status"], "NOT_TESTED")

    def test_editorial_pause_notes_are_not_explicit_pauses(self):
        data = self.prepare(body="朗读者：[语气放慢并稍作停顿]第一句。[语气放慢并稍作停顿]第二句。")
        self.assertEqual(len(data["segments"]), 1)
        observed = v.script_segments(self.script.read_text(), SOLO)
        self.assertEqual(observed[0]["text"], data["segments"][0]["text"])
        self.assertEqual(observed[0]["pause"], 0)

    def test_setting_or_spoken_text_changes_invalidate_review(self):
        data = self.prepare()
        identity = data["text_review"]["input_fingerprint"]
        segment = data["segments"][0]
        segment["spoken_text"] = "首句。"
        with self.assertRaisesRegex(AssertionError, "逐段对照"):
            p.validate_input(self.root, data)
        data["pronunciation_notes"] = [{"segment_id": segment["id"], "original": segment["text"], "spoken": segment["spoken_text"]}]
        self.assertNotEqual(identity, p.validate_input(self.root, data))
        with self.assertRaisesRegex(AssertionError, "尚未核对"):
            asyncio.run(p.synthesize(self.manifest, data, None))
        data["speakers"]["reader"]["voice"] = "zh-CN-YunxiNeural"
        with self.assertRaisesRegex(AssertionError, "声音映射"):
            p.validate_input(self.root, data)

    def test_preflight_budget_survives_restart(self):
        data = self.prepare()
        calls = []
        async def fail():
            calls.append(True)
            raise RuntimeError("离线服务失败")
        async def no_wait(_): pass
        with patch("edge_tts.list_voices", fail), patch("asyncio.sleep", no_wait):
            with self.assertRaisesRegex(RuntimeError, "离线服务失败"):
                asyncio.run(p.preflight(data, [], lambda: p.write_json(self.manifest, data)))
            loaded = json.loads(self.manifest.read_text())
            with self.assertRaisesRegex(RuntimeError, "已用完"):
                asyncio.run(p.preflight(loaded, []))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(loaded["voice_list_attempts"]), 3)

    def test_unavailable_voice_fails_without_replacement(self):
        data = self.prepare()
        async def voices(): return [{"ShortName": "zh-CN-YunxiNeural"}]
        async def no_wait(_): pass
        with patch("edge_tts.list_voices", voices), patch("asyncio.sleep", no_wait):
            with self.assertRaisesRegex(AssertionError, "声线不可用"):
                asyncio.run(p.preflight(data, []))
            with self.assertRaisesRegex(RuntimeError, "已用完"):
                asyncio.run(p.preflight(data, []))
        self.assertEqual(data["speakers"]["reader"]["voice"], "zh-CN-YunyangNeural")

    def test_preflight_cannot_overwrite_generation_versions(self):
        data = self.prepare()
        previous = dict(data["tool_versions"])
        current = {**previous, "edge_tts": "99.0.0"}
        with patch.object(p, "versions", return_value=current), patch("edge_tts.list_voices") as network:
            with self.assertRaisesRegex(AssertionError, "版本已改变"):
                asyncio.run(p.preflight(data, []))
            network.assert_not_called()
        self.assertEqual(data["tool_versions"], previous)

    def test_optional_project_gate_blocks_before_any_network(self):
        self.script.write_text("## 正文\n朗读者：测试。\n")
        p.write_json(self.settings, {"speakers": SOLO, "selection_basis": "测试", "cover": None, "require_script_gate": True})
        data = p.prepare(self.script, self.manifest, self.settings)
        p.review_text(self.manifest, data, "只完成配音文本核对")
        with patch("edge_tts.list_voices") as network:
            with self.assertRaisesRegex(AssertionError, "文字门禁"):
                asyncio.run(p.synthesize(self.manifest, data, None))
            network.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本地 FFmpeg")
    def test_real_pcm_assembly_and_independent_checks_for_short_solo(self):
        data = self.prepare()
        self.make_local_audio(data)
        p.assemble(self.manifest, data)
        report = self.verify()
        self.assertEqual(report["audio_technical_status"], "PASSED")
        self.assertAlmostEqual(report["audio"]["actual_duration_seconds"], 1.05, places=3)
        first = data["segments"][0]
        original = first["spoken_text"]
        first["spoken_text"] = "已改变的内容。"
        data["pronunciation_notes"] = [{"segment_id": first["id"], "original": first["text"], "spoken": first["spoken_text"]}]
        identity = p.fingerprint(p.input_identity(data))
        data["text_review"]["input_fingerprint"] = identity
        p.write_json(self.root / "input_fingerprint.json", {"sha256": identity})
        p.export_tts(self.manifest, data)
        p.write_json(self.manifest, data)
        with self.assertRaisesRegex(ValueError, "缓存不属于当前"):
            self.verify()
        first["spoken_text"] = original
        data["speakers"]["reader"]["voice"] = "zh-CN-YunxiNeural"
        p.write_json(self.manifest, data)
        with self.assertRaisesRegex(ValueError, "用户选择"):
            self.verify()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本地 FFmpeg")
    def test_duration_limits_only_when_requested_and_cache_validation(self):
        data = self.prepare(duration=[720, 1080])
        self.make_local_audio(data)
        p.assemble(self.manifest, data)
        with self.assertRaisesRegex(ValueError, "约定时长"):
            self.verify()
        first = data["segments"][0]
        self.assertTrue(p.check_record(self.root, data, first))
        wav = self.root / first["wav_audio"]["path"]
        wav.write_bytes(wav.read_bytes()[:-20])
        with self.assertRaises(Exception):
            p.check_record(self.root, data, first)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本地 FFmpeg")
    def test_interrupted_success_recovers_without_extra_online_request(self):
        data = self.prepare()
        self.make_local_audio(data)
        for segment in data["segments"]:
            segment["attempts"] = 3
            p.write_json(self.root / "segments" / segment["id"] / "segment.json", segment)
            segment["status"] = "PENDING"
            for key in ("raw_audio", "wav_audio", "input_fingerprint"):
                segment.pop(key)
        p.write_json(self.manifest, data)
        with patch("edge_tts.list_voices") as listing, patch("edge_tts.Communicate") as speech:
            asyncio.run(p.synthesize(self.manifest, data, None))
            listing.assert_not_called()
            speech.assert_not_called()
        loaded = json.loads(self.manifest.read_text())
        self.assertTrue(all(s["status"] == "PASSED" and s["attempts"] == 3 for s in loaded["segments"]))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要本地 FFmpeg")
    def test_sample_covers_all_roles_and_failed_segment_blocks_full(self):
        data = self.prepare(DUAL, "女主持：第一句。\n男主持：第二句。")
        self.make_local_audio(data)
        p.assemble(self.manifest, data, ["s0001"])
        with self.assertRaisesRegex(ValueError, "全部实际角色"):
            self.verify(sample=True)
        p.assemble(self.manifest, data, ["s0001", "s0002"])
        self.assertEqual(self.verify(sample=True)["audio_technical_status"], "PASSED")
        data["segments"][1]["status"] = "FAILED"
        with self.assertRaises(AssertionError):
            p.assemble(self.manifest, data)
        self.assertFalse((self.root / "audio.mp3").exists())

if __name__ == '__main__': unittest.main()
