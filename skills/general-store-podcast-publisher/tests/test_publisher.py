"""离线验证凭据处理、上传恢复及当前发布状态检查。"""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

SPEC = importlib.util.spec_from_file_location(
    "publisher", Path(__file__).resolve().parents[1] / "scripts" / "publish_podcast.py"
)
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.audio = self.root / "episode.mp3"
        self.audio.write_bytes(b"fixture-audio")
        self.plan = {
            "schema_version": 1,
            "run_id": "test-task",
            "base_url": "https://podcast.test",
            "show": {"id": "existing-show"},
            "episode": {"title": "测试集", "summary": "简介"},
            "assets": {
                "audio": {
                    "path": str(self.audio),
                    "filename": self.audio.name,
                    "sha256": p.file_hash(self.audio),
                    "size_bytes": self.audio.stat().st_size,
                }
            },
            "source_quality": {"human_listening_status": "NOT_TESTED"},
        }
        self.receipt = self.root / "receipt.json"

    def client(self, handler):
        client = httpx.Client(
            base_url="https://podcast.test", transport=httpx.MockTransport(handler)
        )
        self.addCleanup(client.close)
        return client

    def test_no_cover_uses_bundled_asset_and_freezes_bytes_for_resume(self):
        selected, source = p.prepare_cover(self.root, None, self.root / "release.json")
        self.assertEqual(source, "default")
        self.assertEqual(selected["sha256"], p.file_hash(p.DEFAULT_COVER))
        self.assertEqual(Path(selected["path"]).parent, self.root / "release.assets")
        self.assertNotEqual(Path(selected["path"]), p.DEFAULT_COVER)
        # 后续技能资源更换时，旧计划仍使用已保存的字节。
        alternate = self.root / "new-default.png"
        alternate.write_bytes(b"different-future-cover")
        with patch.object(p, "DEFAULT_COVER", alternate):
            p.check_files({"assets": {"cover": selected}})

    def test_explicit_cover_has_priority_over_directory_and_default(self):
        (self.root / "cover.png").write_bytes(b"directory-cover")
        explicit = self.root / "custom.jpg"
        explicit.write_bytes(b"custom-cover")
        selected, source = p.prepare_cover(
            self.root, explicit, self.root / "release.json"
        )
        self.assertEqual(source, "explicit")
        self.assertEqual(selected["sha256"], p.file_hash(explicit))

    def test_directory_cover_is_used_when_no_explicit_cover(self):
        local = self.root / "cover.png"
        local.write_bytes(b"local-cover")
        selected, source = p.prepare_cover(self.root, None, self.root / "release.json")
        self.assertEqual(source, "input_directory")
        self.assertEqual(selected["path"], str(local))

    def test_invalid_explicit_cover_does_not_silently_fall_back(self):
        with self.assertRaises(p.PublishError):
            p.prepare_cover(
                self.root, self.root / "missing.png", self.root / "release.json"
            )

    def test_retry_reopens_upload_and_reuses_idempotency_key(self):
        seen = []

        def handler(request):
            seen.append((request.read(), request.headers["Idempotency-Key"]))
            return httpx.Response(503 if len(seen) == 1 else 201, json={"id": "asset"})

        pub = p.Publisher(self.client(handler), self.plan, self.receipt, "test-key")
        with patch.object(p.time, "sleep"):
            result = pub.write(
                "upload-audio", p.PREFIX + "/assets", file=str(self.audio)
            )
        self.assertEqual(result["id"], "asset")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(seen[0][0], self.audio.read_bytes())

    def test_restart_reuses_successful_receipt_without_network(self):
        pub = p.Publisher(
            self.client(lambda r: httpx.Response(201, json={"id": "episode"})),
            self.plan,
            self.receipt,
            "test-key",
        )
        pub.write("create", p.PREFIX + "/episodes", payload={"title": "测试"})

        def no_network(request):
            self.fail("成功的步骤不应重发")

        resumed = p.Publisher(
            self.client(no_network), self.plan, self.receipt, "test-key"
        )
        self.assertEqual(
            resumed.write("create", p.PREFIX + "/episodes", payload={"title": "测试"}),
            {"id": "episode"},
        )

    def test_ambiguous_failure_keeps_intent_and_same_key_on_restart(self):
        keys = []

        def broken(request):
            keys.append(request.headers["Idempotency-Key"])
            raise httpx.ReadTimeout("fixture", request=request)

        pub = p.Publisher(self.client(broken), self.plan, self.receipt, "test-key")
        with patch.object(p.time, "sleep"), self.assertRaises(p.PublishError):
            pub.write(
                "publish",
                p.PREFIX + "/episodes/id/publish",
                payload={"expected_version": 1},
            )
        self.assertEqual(len(keys), 4)
        self.assertNotIn("result", p.read_json(self.receipt)["steps"]["publish"])

        def recovered(request):
            keys.append(request.headers["Idempotency-Key"])
            return httpx.Response(200, json={"id": "episode", "version": 2})

        resumed = p.Publisher(
            self.client(recovered), self.plan, self.receipt, "test-key"
        )
        resumed.write(
            "publish",
            p.PREFIX + "/episodes/id/publish",
            payload={"expected_version": 1},
        )
        self.assertEqual(len(set(keys)), 1)

    def test_401_409_422_and_redirect_are_not_retried(self):
        for status in (401, 403, 409, 422, 302):
            calls = []

            def denied(request):
                calls.append(request)
                return httpx.Response(status, json={"secret": "must-not-print"})

            pub = p.Publisher(
                self.client(denied), self.plan, self.root / f"{status}.json", "test-key"
            )
            with self.assertRaises(p.PublishError) as result:
                pub.request("test", p.PREFIX + "/episodes")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("must-not-print", str(result.exception))

    def test_changed_plan_or_key_cannot_resume(self):
        pub = p.Publisher(
            self.client(lambda r: None), self.plan, self.receipt, "test-key"
        )
        pub.save()
        changed = copy.deepcopy(self.plan)
        changed["episode"]["title"] = "different"
        with self.assertRaises(p.PublishError):
            p.Publisher(pub.client, changed, self.receipt, "test-key")
        with self.assertRaises(p.PublishError):
            p.Publisher(pub.client, self.plan, self.receipt, "another-key")

    def test_changed_source_is_detected_before_publish(self):
        transcript = self.root / "transcript.md"
        transcript.write_text("原稿")
        self.plan["assets"] = {"transcript": p.asset(transcript, "transcript")}
        transcript.write_text("新稿")
        with self.assertRaises(p.PublishError):
            p.check_files(self.plan)

    def test_credentials_formats_do_not_execute_shell(self):
        key, secret = "gsk_testvalue", "testsecret"
        for content in (
            json.dumps(
                {"GENERAL_STORE_API_KEY": key, "GENERAL_STORE_API_SECRET": secret}
            ),
            f"GENERAL_STORE_API_KEY='{key}'\nGENERAL_STORE_API_SECRET={secret}",
            f"{key}\n{secret}\n",
        ):
            path = self.root / "credentials"
            path.write_text(content)
            self.assertEqual(p.credentials(path), (key, secret))
        path.write_text("touch /tmp/should-not-be-created-by-publisher")
        with self.assertRaises(p.PublishError):
            p.credentials(path)

    def test_url_restrictions_and_local_opt_in(self):
        for base in (
            "http://remote.test",
            "https://a:b@podcast.test",
            "https://podcast.test/path",
            "https://podcast.test/?key=value",
        ):
            with self.assertRaises(p.PublishError):
                p.origin(base, True)
        self.assertEqual(
            p.origin("http://127.0.0.1:8765", True), "http://127.0.0.1:8765"
        )
        with self.assertRaises(p.PublishError):
            p.origin("http://127.0.0.1:8765")

    def test_verify_rejects_changed_publication_pointer(self):
        pub = p.Publisher(
            self.client(
                lambda request: httpx.Response(
                    200,
                    json=(
                        {
                            "id": "asset",
                            "kind": "audio",
                            "status": "ready",
                            **self.plan["assets"]["audio"],
                        }
                        if "/assets/" in request.url.path
                        else {
                            "id": "existing-show",
                            "status": "published",
                            "current_publication_id": "show-publication",
                        }
                        if "/shows/" in request.url.path
                        else {
                            "id": "episode",
                            "title": "测试集",
                            "summary": "简介",
                            "show_id": "existing-show",
                            "status": "published",
                            "current_publication_id": "changed",
                            "audio_asset_id": "asset",
                            "cover_asset_id": None,
                            "transcript_asset_id": None,
                            "has_unpublished_changes": False,
                        }
                    ),
                )
            ),
            self.plan,
            self.receipt,
            "test-key",
        )
        pub.state["steps"] = {
            "upload-audio": {"result": {"id": "asset"}},
            "episode-create": {"intent": {"payload": {"show_id": "existing-show"}}},
            "episode-publish": {
                "result": {"id": "episode", "current_publication_id": "original"}
            },
        }
        with self.assertRaises(p.PublishError):
            pub.verify()


if __name__ == "__main__":
    unittest.main()
