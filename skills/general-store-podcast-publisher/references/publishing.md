# 发布命令与契约

这里的 `python` 指安装了 httpx 的 Python 3.10+；`SKILL_DIR` 指当前技能绝对目录。计划与回执由用户工作目录保存，不能存进 skill 目录或提交进仓库。

## 凭据

环境变量：`GENERAL_STORE_API_KEY`、`GENERAL_STORE_API_SECRET`。或者传 `--credentials-file /absolute/path/pingzheng`，接受三种形式：

- JSON 对象，键为上述两个环境变量名；
- 简单 `GENERAL_STORE_API_KEY=<Key>` / `GENERAL_STORE_API_SECRET=<Secret>`；
- 第一行 Key（`gsk_` 开头），第二行 Secret。

文件只作数据读取，不执行命令，不展开 shell 变量。JSON/env 可以含 `GENERAL_STORE_BASE_URL`，但目标地址始终以 `--base-url` 及随后固定计划为准，避免凭据配置悄悄改目标。环境中的 Secret 不会输出。1Password 等凭据管理器可在用户授权后向子进程环境注入，不把原文带入工具结果。

```bash
python "$SKILL_DIR/scripts/publish_podcast.py" list-shows \
  --base-url https://my.goodlearnings.cn \
  --credentials-file /absolute/path/pingzheng

python "$SKILL_DIR/scripts/publish_podcast.py" prepare \
  --input-dir /absolute/path/episode-output \
  --base-url https://my.goodlearnings.cn \
  --show-id <查询到的节目UUID> \
  --plan /absolute/path/release/plan.json

python "$SKILL_DIR/scripts/publish_podcast.py" publish \
  --plan /absolute/path/release/plan.json \
  --receipt /absolute/path/release/receipt.json \
  --credentials-file /absolute/path/pingzheng --execute

python "$SKILL_DIR/scripts/publish_podcast.py" verify \
  --plan /absolute/path/release/plan.json \
  --receipt /absolute/path/release/receipt.json \
  --credentials-file /absolute/path/pingzheng
```

新建节目用 `--show-name '节目名' --author '作者'` 替代 `--show-id`，可加 `--show-summary`。覆盖自动提取值使用 `--title`、`--summary`、`--audio`、`--transcript`、`--cover`；自选文字稿应说明使用清洁朗读稿还是含参考资料的正式稿。

封面选择优先级：`--cover` → 素材目录中的 `cover.png`、`cover.jpg`、`cover.jpeg`、`podcast_cover.png`、`podcast_cover.jpg`、`podcast_cover.jpeg`（按此顺序）→ 技能内置 `assets/default-cover.png`。新计划始终包含封面，不再依赖节目封面继承。`cover_source` 会记录 `explicit`、`input_directory` 或 `default`。默认图复制到计划旁 `<计划名>.assets/`，文件名含完整 SHA-256；保留此目录即可在技能升级后继续使用原图续跑。已存在计划保持原样，不自动增加封面；指定但不存在或格式无效的封面会报错。

`prepare` 不访问网络、不需要密钥、不覆盖已存在计划；先完成并审阅，再 `publish --execute`。`--execute` 表示调用方已经确认用户授权，不是独立授权来源。本地测试只有明确传 `--allow-local-http` 才允许 localhost、127.0.0.1、::1 HTTP，prepare 与后续命令均须传入。

## 当前接口

截至 2026-09-16，已对照 General Store 本地实现的 `services/api/app/api/routes/podcasts.py`、`app/auth/api_keys.py` 及 `docs/播客API密钥使用说明.md` 核实。部署契约变化时优先核对当前实现。

- 认证请求头为 `X-API-Key` 和 `X-API-Secret`，不混用 Cookie、Bearer、CSRF。仅 `/v1/admin/podcasts/*` 接受双凭据；创建者需有效授权和 `podcast:write` 权限。
- 只用 HTTPS；校验证书、不跟随重定向、不继承环境代理。音频上传可能耗时，读写超时 660 秒；网络、429、5xx 最多四次，退避 1/2/4 秒，尊重数字 Retry-After 且上限 60 秒。
- `GET /shows?limit=100&offset=0&q=...` 查询节目；`GET /episodes?show_id=<id>&q=<标题>&limit=100` 查询单集。前缀均为 `/v1/admin/podcasts`，结果含 `items/total/limit/offset`。
- `POST /assets?kind=audio|cover|transcript&filename=...` 使用原始字节 `application/octet-stream`，不是 multipart。服务端资源须为 ready，哈希和大小与本地一致。
- 音频 MP3/AAC M4A 最大 500 MiB；封面 PNG/JPEG 最大 10 MiB；文字稿 UTF-8 TXT/Markdown 最大 1 MiB。文字稿/封面可选，服务端最终校验实际内容。
- 新节目 `POST /shows`，字段 `name/author/summary/cover_asset_id`；新单集 `POST /episodes`，字段 `show_id/title/summary/audio_asset_id/cover_asset_id/transcript_asset_id`。
- `POST /shows/{id}/publish` 和 `POST /episodes/{id}/publish` 需要 `expected_version`。发布产生不可变快照；单集依赖已发布节目与就绪音频。
- 每个写请求带稳定的 `Idempotency-Key`。幂等结果按 API Key 隔离；响应是历史操作回执，当前状态需 GET 回读。

## 验收口径

脚本回读所有上传资源的 SHA-256/大小/ready、节目已发布状态、单集标题/简介/资源关联、无未发布修改及当前快照指针。输出 `PUBLISHED_VERIFIED` 仅表示这些管理 API 检查通过；`reader_playback_status=NOT_TESTED` 固定保留，人工听审状态沿用素材记录或 UNKNOWN。

回执包含本地路径、文字稿上传后的服务端内容和操作元数据；不含 Key/Secret。按私有工作产物保存，不公开分享整个回执。
