# 默认封面生成记录

生成日期：2026-09-16。使用内置 imagegen 工具，未调用 CLI/API fallback。

资源位置：`../assets/default-cover.png`（相对本说明所在目录）。实际图片为 1254 × 1254 RGB PNG，1,801,222 字节；保留生成原图，无缩放、裁剪或重新编码。

SHA-256：`4d6fe32456d6e0ad32b4fadf1cc90f8825cd6339debe58c2edd18a6f5088b02f`。

视觉：居中头戴式耳机、柔和声波光晕、无文字无商标；方形完整画面，便于作为不同主题单集的通用封面。

## 原始提示词

```text
Use case: stylized-concept
Asset type: production-ready universal podcast default cover artwork.
Primary request: Create one elegant square cover image for a personal podcast app, to be used whenever an episode has no supplied cover. 1024x1024 square, full bleed artwork.
Subject and composition: a single sculptural pair of over-ear headphones, centered with generous breathing space, with a subtle abstract sound ripple suggesting attentive listening. Recognizable at very small thumbnail sizes. Understated editorial art direction, polished tactile 3D illustration, soft light and calm inviting atmosphere, simple coherent background, clear silhouette and balanced contrast.
Constraints: no text, no letters, no numbers, no logo, no watermark, no people, no brand, no UI, no device mockup, no rounded outer corners, no frame. This is the finished cover artwork itself, not a presentation of a cover.
```

提示词要求 1024 方图，工具实际返回 1254 方图；其大小、像素数与 PNG 格式符合 General Store 封面上传约束。

## 接入验证

13 项离线测试及 1 项真实 FastAPI 隔离集成测试通过。集成测试使用 office_agents_control 的完整音频、文字稿和此默认封面，验证上传、发布、回读哈希及重复续跑；重复运行后仍为 3 个资源、1 个节目、1 个单集、1 个快照。显式封面优先、目录封面识别、缺省回退、默认图字节保存及错误显式封面停止均已覆盖。

未修改既有线上单集或其历史发布计划。
