# video-creater 🎬

开源的本地视频生成 Agent 客户端。自带模型 API 与 key、注入自己的系统提示词与
skill，从几句话 / 几张图 / 一段视频开始，一步步生成素材并最终产出视频。过程数据
与日志全程可查看，可随时回到任意一步继续。

设计文档：[PRD (issue #1)](https://github.com/moux1024/video-creater/issues/1) ·
[CONTEXT.md](CONTEXT.md) · [ADR 0001](docs/adr/0001-immutable-runs-with-parent-chain.md) ·
[ADR 0002](docs/adr/0002-json-convention-over-function-calling.md)

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e . sse-starlette tomli-w uvicorn
npm --prefix ui install && npm --prefix ui run build   # 前端（首次）
.venv/bin/python -m uvicorn app.server:app --port 8642
# 打开 http://127.0.0.1:8642
```

## 核心概念

- **固定阶段流水线**：意图理解 → 分镜/脚本 → 逐镜头 Prompt → 首帧图 → 视频生成 → 拼接导出。
- **按角色配置模型**：Agent 大脑 / 文本 / 图像 / 视频，各自可填任意 OpenAI 兼容 endpoint + key（配置存 `~/.video-creater/config.toml`）。
- **提示词分层**：内置基础 → 全局 → 项目 → 激活 skill → 阶段覆盖，追加拼接、后者优先；每次调用的组装结果写入 run 日志（`system_prompt_assembled` 事件）。
- **Skill 即 Markdown**：项目 `skills/` 目录下的 `.md` 文件，界面勾选激活。
- **不可变 Run + 溯源链**：重跑永远新建 run；run 记录 parent 链；上游切换版本后下游显示 stale（仅提示不阻断）。
- **文件即数据库**：每个项目一个目录（`project.json` / `artifacts` / 每 run 的 `events.ndjson` + `log.txt`）。
- **视频供应商适配**：内置 kling / jimeng / vidu / minimax 参考实现，实现 `submit / poll / download` 三方法即可扩展（`app/video_provider/`）。
- **导出**：ffmpeg 按镜头顺序拼接；无 ffmpeg 时自动降级为素材包导出。

## 开发

```bash
.venv/bin/python -m pytest tests/ -q      # 后端单测
npm --prefix ui run dev                   # 前端开发（代理 /api 到 8642）
```

License: Apache-2.0
