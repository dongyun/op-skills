#!/usr/bin/env python3
"""用 Key＋Secret 预检、发布并回读 General Store 播客；Python 3.10+ / httpx。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

PREFIX = "/v1/admin/podcasts"
DEFAULT_COVER = Path(__file__).resolve().parents[1] / "assets" / "default-cover.png"
COVER_FILENAMES = (
    "cover.png",
    "cover.jpg",
    "cover.jpeg",
    "podcast_cover.png",
    "podcast_cover.jpg",
    "podcast_cover.jpeg",
)
LIMITS = {"audio": 500 * 1024**2, "cover": 10 * 1024**2, "transcript": 1024**2}
EXTENSIONS = {
    "audio": {".mp3", ".m4a"},
    "cover": {".jpg", ".jpeg", ".png"},
    "transcript": {".txt", ".md", ".markdown"},
}


class PublishError(Exception):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def origin(value, local=False):
    parsed = urlsplit(value.rstrip("/"))
    local_http = (
        local
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if (
        not parsed.hostname
        or (parsed.scheme != "https" and not local_http)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise PublishError("地址必须为 HTTPS 源站；本地测试可显式允许 loopback HTTP。")
    return value.rstrip("/")


def credentials(path=None):
    values = {}
    if path:
        target = Path(path).expanduser().resolve()
        if not stat.S_ISREG(target.stat().st_mode):
            raise PublishError("凭据路径必须是普通文件。")
        # 支持 JSON、KEY=value、Key/Secret 两行；不执行 shell，不展开变量。
        text = target.read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if text.lstrip().startswith("{"):
            values = json.loads(text)
        elif len(lines) == 2 and lines[0].startswith("gsk_") and "=" not in lines[0]:
            values = {
                "GENERAL_STORE_API_KEY": lines[0],
                "GENERAL_STORE_API_SECRET": lines[1],
            }
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                name, separator, value = line.partition("=")
                if not separator:
                    raise PublishError(
                        "凭据文件应为 JSON 或 KEY=value；不接受 shell 脚本。"
                    )
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[name.strip()] = value
    names = ("GENERAL_STORE_API_KEY", "GENERAL_STORE_API_SECRET")
    pair = tuple(values.get(name, os.environ.get(name, "")) for name in names)
    if any(
        not isinstance(value, str)
        or not value.strip()
        or not value.isascii()
        or any(c.isspace() or ord(c) < 32 for c in value)
        for value in pair
    ):
        raise PublishError(
            "缺少或无效的 GENERAL_STORE_API_KEY / GENERAL_STORE_API_SECRET。"
        )
    return pair


def asset(path, kind):
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in EXTENSIONS[kind]:
        raise PublishError(f"{kind} 文件不存在或格式不支持。")
    size = path.stat().st_size
    if not 0 < size <= LIMITS[kind]:
        raise PublishError(f"{kind} 文件为空或超过接口大小上限。")
    if kind == "transcript":
        source = path.read_text(encoding="utf-8-sig")
        if not source.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in source):
            raise PublishError("文字稿为空或含非法控制字符。")
    result = {
        "path": str(path),
        "filename": path.name,
        "size_bytes": size,
        "sha256": file_hash(path),
    }
    if kind == "audio" and shutil.which("ffprobe"):
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,codec_type:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if probe.returncode:
            raise PublishError("ffprobe 无法读取音频。")
        data = json.loads(probe.stdout)
        streams = [s for s in data.get("streams", []) if s["codec_type"] == "audio"]
        duration = float(data.get("format", {}).get("duration", 0))
        if (
            len(streams) != 1
            or streams[0]["codec_name"] not in {"mp3", "aac"}
            or duration <= 0
        ):
            raise PublishError("音频必须只有一条 MP3/AAC 音轨且时长为正。")
        result["duration_seconds"] = duration
    return result


def text_field(value, label, maximum, empty=False):
    if (
        not isinstance(value, str)
        or (not empty and not value.strip())
        or len(value) > maximum
    ):
        raise PublishError(f"{label} 不能为空或超过 {maximum} 字。")
    return value.strip()


def choose_cover(root, explicit):
    if explicit is not None:
        return explicit, "explicit"
    for name in COVER_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate, "input_directory"
    if not DEFAULT_COVER.is_file():
        raise PublishError("内置默认封面缺失，请修复技能安装或使用 --cover 指定封面。")
    return DEFAULT_COVER, "default"


def prepare_cover(root, explicit, plan_path):
    path, source = choose_cover(root, explicit)
    result = asset(path, "cover")
    if source == "default":
        # 为此计划保留原始字节，技能升级或换默认图不影响进行中的续跑。
        folder = plan_path.resolve().parent / (plan_path.stem + ".assets")
        folder.mkdir(parents=True, exist_ok=True)
        frozen = folder / ("default-cover-" + result["sha256"] + ".png")
        if not frozen.exists():
            with (
                Path(result["path"]).open("rb") as original,
                frozen.open("xb") as output,
            ):
                shutil.copyfileobj(original, output)
        if file_hash(frozen) != result["sha256"]:
            raise PublishError("计划中的默认封面副本不一致；已停止，请检查文件。")
        result = asset(frozen, "cover")
    return result, source


def prepare(args):
    if args.plan.exists():
        raise PublishError("计划文件已存在；继续使用原计划，或为新任务选择新路径。")
    root = args.input_dir.expanduser().resolve()
    delivery = (
        read_json(root / "delivery.json") if (root / "delivery.json").is_file() else {}
    )
    title = text_field(args.title or delivery.get("topic", ""), "单集标题", 200)
    summary = args.summary
    if summary is None:
        script = root / "podcast_script.md"
        match = re.search(
            r"^## 本集一句话\s*\n(.*?)(?=^## |\Z)",
            script.read_text(encoding="utf-8") if script.exists() else "",
            re.M | re.S,
        )
        summary = match[1].strip() if match else ""
    summary = text_field(summary, "单集简介", 2000, empty=True)
    files = {"audio": asset(args.audio or root / "audio.mp3", "audio")}
    transcript = args.transcript or next(
        (
            root / name
            for name in ("podcast_script_tts.md", "podcast_script.md")
            if (root / name).is_file()
        ),
        None,
    )
    if transcript:
        files["transcript"] = asset(transcript, "transcript")
    files["cover"], cover_source = prepare_cover(root, args.cover, args.plan)
    if args.show_id:
        show = {"id": str(uuid.UUID(args.show_id))}
    else:
        show = {
            "name": text_field(args.show_name, "节目名", 200),
            "author": text_field(args.author, "作者", 200),
            "summary": text_field(args.show_summary, "节目简介", 2000, empty=True),
        }
    plan = {
        "schema_version": 1,
        "run_id": args.run_id or str(uuid.uuid4()),
        "base_url": origin(args.base_url, args.allow_local_http),
        "show": show,
        "episode": {"title": title, "summary": summary},
        "assets": files,
        "cover_source": cover_source,
        "source_quality": {
            name: delivery.get(name, "UNKNOWN")
            for name in (
                "script_status",
                "audio_technical_status",
                "human_listening_status",
            )
        },
    }
    if args.plan.exists():
        raise PublishError("计划文件已存在；继续使用原计划，或为新任务选择新路径。")
    write_json(args.plan, plan)
    return {"plan": str(args.plan.resolve()), **plan}


@contextmanager
def receipt_lock(path):
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublishError("同一回执已有发布进程运行。") from None
        yield


class Publisher:
    def __init__(self, client, plan, receipt_path, key):
        self.client, self.plan, self.receipt_path = client, plan, Path(receipt_path)
        binding = {
            "plan_sha256": digest(plan),
            "api_key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        }
        self.state = (
            read_json(receipt_path)
            if self.receipt_path.exists()
            else {
                **binding,
                "run_id": plan["run_id"],
                "base_url": plan["base_url"],
                "steps": {},
            }
        )
        if any(self.state.get(name) != value for name, value in binding.items()):
            raise PublishError(
                "计划或 API Key 已变更；不能复用此发布回执。请先核对远端结果。"
            )

    def save(self):
        write_json(self.receipt_path, self.state)

    def request(
        self, step, path, *, payload=None, file=None, params=None, method="POST"
    ):
        headers = (
            {}
            if method == "GET"
            else {
                "Idempotency-Key": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"general-store-podcast:{self.plan['run_id']}:{step}",
                    )
                )
            }
        )
        for attempt in range(4):
            response = None
            try:
                if file:
                    with Path(file).open("rb") as stream:
                        response = self.client.request(
                            method,
                            path,
                            params=params,
                            content=stream,
                            headers={
                                **headers,
                                "Content-Type": "application/octet-stream",
                            },
                        )
                else:
                    response = self.client.request(
                        method, path, json=payload, params=params, headers=headers
                    )
            except httpx.TransportError:
                pass
            if response is not None:
                if 200 <= response.status_code < 300:
                    try:
                        result = response.json()
                    except ValueError:
                        raise PublishError(
                            f"{step} 返回无效 JSON；保留回执后重试。"
                        ) from None
                    if not isinstance(result, dict):
                        raise PublishError(f"{step} 返回结构不符合接口契约。")
                    return result
                if response.status_code != 429 and response.status_code < 500:
                    # 不打印响应正文、请求头、签名 URL 或凭据。
                    raise PublishError(
                        f"{step} HTTP {response.status_code}；已停止，未跟随重定向。"
                    )
            if attempt == 3:
                raise PublishError(
                    f"{step} 四次尝试未成功；保留计划、回执及凭据后重试。"
                )
            delay = 2**attempt
            if (
                response is not None
                and response.headers.get("Retry-After", "").isdigit()
            ):
                delay = min(60, max(delay, int(response.headers["Retry-After"])))
            time.sleep(delay)
        raise PublishError("请求未完成。")

    def get(self, path, params=None):
        return self.request("回读", path, method="GET", params=params)

    def write(self, step, path, *, payload=None, file=None, params=None):
        intent = {"path": path, "payload": payload, "params": params, "file": file}
        previous = self.state["steps"].get(step)
        if previous is not None:
            if previous["intent"] != intent:
                raise PublishError(f"{step} 输入已变更；停止恢复。")
            if "result" in previous:
                return previous["result"]
        else:
            previous = self.state["steps"][step] = {"intent": intent}
            self.save()  # 先保存意图；响应丢失时版本和幂等键仍保持原值。
        result = self.request(step, path, payload=payload, file=file, params=params)
        previous["result"] = result
        self.save()
        return result

    def verify_assets(self):
        for kind, expected in self.plan["assets"].items():
            uploaded = self.state["steps"][f"upload-{kind}"]["result"]
            current = self.get(f"{PREFIX}/assets/{uploaded['id']}")
            if any(
                current.get(field) != value
                for field, value in {
                    "kind": kind,
                    "status": "ready",
                    "sha256": expected["sha256"],
                    "size_bytes": expected["size_bytes"],
                }.items()
            ):
                raise PublishError(f"{kind} 远端资源与计划不一致。")

    def publish(self):
        # 在任何写入前确认凭据可用，已有节目处于已发布状态。
        if "id" in self.plan["show"]:
            show = self.get(f"{PREFIX}/shows/{self.plan['show']['id']}")
            if show.get("status") != "published" or not show.get(
                "current_publication_id"
            ):
                raise PublishError("目标节目未发布；本工具不擅自发布已有节目的草稿。")
        else:
            self.get(f"{PREFIX}/shows", params={"limit": 1})
            show = None
        assets = {}
        for kind, spec in self.plan["assets"].items():
            result = self.write(
                f"upload-{kind}",
                f"{PREFIX}/assets",
                file=spec["path"],
                params={"kind": kind, "filename": spec["filename"]},
            )
            if (
                result.get("status") != "ready"
                or result.get("sha256") != spec["sha256"]
            ):
                raise PublishError(f"{kind} 上传结果未就绪或哈希不一致。")
            assets[f"{kind}_asset_id"] = result["id"]
        self.verify_assets()
        if show is None:
            body = {**self.plan["show"], "cover_asset_id": assets.get("cover_asset_id")}
            show = self.write("show-create", f"{PREFIX}/shows", payload=body)
            self.write(
                "show-publish",
                f"{PREFIX}/shows/{show['id']}/publish",
                payload={"expected_version": show["version"]},
            )
        episode = self.write(
            "episode-create",
            f"{PREFIX}/episodes",
            payload={**self.plan["episode"], **assets, "show_id": show["id"]},
        )
        self.write(
            "episode-publish",
            f"{PREFIX}/episodes/{episode['id']}/publish",
            payload={"expected_version": episode["version"]},
        )
        return self.verify()

    def verify(self):
        published = self.state["steps"].get("episode-publish", {}).get("result")
        if not published:
            raise PublishError("回执尚无单集发布成功记录；请用 publish 恢复。")
        self.verify_assets()
        episode = self.get(f"{PREFIX}/episodes/{published['id']}")
        show_id = self.state["steps"]["episode-create"]["intent"]["payload"]["show_id"]
        show = self.get(f"{PREFIX}/shows/{show_id}")
        expected = {
            **self.plan["episode"],
            "show_id": show_id,
            "status": "published",
            "current_publication_id": published["current_publication_id"],
            "has_unpublished_changes": False,
        }
        for kind in ("audio", "transcript", "cover"):
            result = self.state["steps"].get(f"upload-{kind}", {}).get("result")
            expected[f"{kind}_asset_id"] = result["id"] if result else None
        if (
            any(episode.get(field) != value for field, value in expected.items())
            or show.get("status") != "published"
            or not show.get("current_publication_id")
        ):
            raise PublishError("当前单集或节目与发布回执不一致；不要覆盖远端变更。")
        report = {
            "status": "PUBLISHED_VERIFIED",
            "show_id": show_id,
            "episode_id": episode["id"],
            "publication_id": episode["current_publication_id"],
            "title": episode["title"],
            "version": episode["version"],
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "reader_playback_status": "NOT_TESTED",
            "human_listening_status": self.plan["source_quality"][
                "human_listening_status"
            ],
        }
        self.state["verification"] = report
        self.save()
        return report


def check_files(plan):
    for kind, spec in plan["assets"].items():
        current = asset(spec["path"], kind)
        if any(
            current[field] != spec[field]
            for field in ("filename", "size_bytes", "sha256")
        ):
            raise PublishError(f"{kind} 输入文件已变更；未发送任何请求。")


def make_client(base, pair):
    return httpx.Client(
        base_url=base,
        headers={"X-API-Key": pair[0], "X-API-Secret": pair[1]},
        timeout=httpx.Timeout(660, connect=15),
        trust_env=False,
        follow_redirects=False,
        verify=True,
    )


def parser():
    main = argparse.ArgumentParser(description=__doc__)
    sub = main.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="本地生成可审阅计划；不需要凭据，不访问网络")
    prep.add_argument("--input-dir", type=Path, required=True)
    prep.add_argument("--plan", type=Path, required=True)
    prep.add_argument("--base-url", required=True)
    prep.add_argument("--allow-local-http", action="store_true")
    prep.add_argument("--run-id")
    group = prep.add_mutually_exclusive_group(required=True)
    group.add_argument("--show-id")
    group.add_argument("--show-name")
    prep.add_argument("--author")
    prep.add_argument("--show-summary", default="")
    prep.add_argument("--title")
    prep.add_argument("--summary")
    for name in ("audio", "cover", "transcript"):
        prep.add_argument(f"--{name}", type=Path)
    for command in ("publish", "verify"):
        operation = sub.add_parser(command)
        operation.add_argument("--plan", type=Path, required=True)
        operation.add_argument("--receipt", type=Path, required=True)
        operation.add_argument("--credentials-file", type=Path)
        operation.add_argument("--allow-local-http", action="store_true")
        if command == "publish":
            operation.add_argument(
                "--execute",
                action="store_true",
                required=True,
                help="用户已授权此目标发布后使用",
            )
    listing = sub.add_parser("list-shows", help="只读列出所有节目，可按关键词过滤")
    listing.add_argument("--base-url", required=True)
    listing.add_argument("--credentials-file", type=Path)
    listing.add_argument("--allow-local-http", action="store_true")
    listing.add_argument("--query", default="")
    return main


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args)
        elif args.command == "list-shows":
            pair = credentials(args.credentials_file)
            with make_client(
                origin(args.base_url, args.allow_local_http), pair
            ) as client:
                # 列表不创建回执；请求仍使用同样的认证和有限重试行为。
                publisher = Publisher(
                    client,
                    {"run_id": "list", "base_url": args.base_url},
                    Path(tempfile.gettempdir()) / f"unused-{uuid.uuid4()}",
                    pair[0],
                )
                items, offset = [], 0
                while True:
                    data = publisher.get(
                        f"{PREFIX}/shows",
                        params={"limit": 100, "offset": offset, "q": args.query},
                    )
                    items.extend(
                        {
                            k: item.get(k)
                            for k in (
                                "id",
                                "name",
                                "author",
                                "status",
                                "version",
                                "has_unpublished_changes",
                            )
                        }
                        for item in data["items"]
                    )
                    offset += len(data["items"])
                    if offset >= data["total"] or not data["items"]:
                        break
                result = {"items": items, "total": len(items)}
        else:
            plan = read_json(args.plan)
            base = origin(plan["base_url"], args.allow_local_http)
            if plan.get("schema_version") != 1:
                raise PublishError("不支持的计划版本。")
            if args.command == "publish":
                check_files(plan)
            pair = credentials(args.credentials_file)
            with receipt_lock(args.receipt), make_client(base, pair) as client:
                publisher = Publisher(client, plan, args.receipt, pair[0])
                result = (
                    publisher.publish()
                    if args.command == "publish"
                    else publisher.verify()
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except PublishError as exc:
        print(f"已停止：{exc}", file=sys.stderr)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        httpx.HTTPError,
    ):
        # 凭据解析错误可能含原文，避免输出异常对象。
        print(
            "已停止：文件、计划结构或连接异常；请核对输入，凭据和响应正文未输出。",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
