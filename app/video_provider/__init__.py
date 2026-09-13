"""VideoProvider adaptation layer — unified submit / poll / download interface.

Implementations are best-effort reference code against publicly documented
endpoints; each takes credentials from the ModelConfig (role=video).
Community PRs welcome — implement the three methods and register in PROVIDERS.
"""
from __future__ import annotations

import abc
import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field

import httpx

from ..config import ModelConfig


@dataclass
class VideoTask:
    provider_task_id: str
    status: str                      # submitted | rendering | done | failed
    video_url: str = ""
    error: str = ""


@dataclass
class SubmitOptions:
    prompt: str
    duration_seconds: int = 5
    aspect_ratio: str = "16:9"
    first_frame_path: str = ""       # optional image-to-video source
    extra: dict = field(default_factory=dict)


class VideoProvider(abc.ABC):
    name: str = "base"

    def __init__(self, cfg: ModelConfig, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.http = http or httpx.AsyncClient(timeout=60.0)

    @abc.abstractmethod
    async def submit(self, opts: SubmitOptions) -> VideoTask: ...

    @abc.abstractmethod
    async def poll(self, task: VideoTask) -> VideoTask: ...

    async def download(self, task: VideoTask, out_path) -> None:
        resp = await self.http.get(task.video_url)
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(resp.content)


# ---------------------------------------------------------------- kling
class KlingProvider(VideoProvider):
    """Kling (可灵) open platform — JWT signed with access key / secret key."""
    name = "kling"
    BASE = "https://api.klingai.com"

    def _jwt(self) -> str:
        ak = self.cfg.api_key
        sk = self.cfg.extra.get("secret_key", "")
        headers = {"alg": "HS256", "typ": "JWT"}
        now = int(time.time())
        payload = {"iss": ak, "exp": now + 1800, "nbf": now - 5}
        # compact manual JWT (no external dep)
        def b64(o: dict) -> str:
            return base64.urlsafe_b64encode(
                json.dumps(o, separators=(",", ":")).encode()).rstrip(b"=").decode()
        signing = f"{b64(headers)}.{b64(payload)}"
        sig = hmac.new(sk.encode(), signing.encode(), hashlib.sha256).digest()
        return f"{signing}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"

    async def submit(self, opts: SubmitOptions) -> VideoTask:
        mode = "image2video" if opts.first_frame_path else "text2video"
        image: str | None = None
        if opts.first_frame_path:
            image = base64.b64encode(open(opts.first_frame_path, "rb").read()).decode()
        body = {
            "model_name": self.cfg.model or "kling-v1",
            "prompt": opts.prompt,
            "duration": str(opts.duration_seconds),
            "aspect_ratio": opts.aspect_ratio,
            **({"image": image} if image else {}),
        }
        resp = await self.http.post(
            f"{self.BASE}/v1/videos/{mode}",
            json=body, headers={"Authorization": f"Bearer {self._jwt()}"})
        data = resp.json()
        return VideoTask(provider_task_id=data["data"]["task_id"], status="submitted")

    async def poll(self, task: VideoTask) -> VideoTask:
        resp = await self.http.get(
            f"{self.BASE}/v1/videos/{task.provider_task_id}",
            headers={"Authorization": f"Bearer {self._jwt()}"})
        data = resp.json()["data"]
        status = data["task_status"]  # submitted / processing / succeed / failed
        mapping = {"succeed": "done", "processing": "rendering",
                   "submitted": "submitted", "failed": "failed"}
        return VideoTask(
            provider_task_id=task.provider_task_id,
            status=mapping.get(status, "rendering"),
            video_url=(data.get("task_result", {}).get("videos") or [{}])[0].get("url", ""),
            error=data.get("task_status_msg", ""))


# ---------------------------------------------------------------- vidu
class ViduProvider(VideoProvider):
    name = "vidu"
    BASE = "https://api.vidu.com/ent/v2"

    async def submit(self, opts: SubmitOptions) -> VideoTask:
        body = {
            "model": self.cfg.model or "viduq1",
            "prompt": opts.prompt,
            "duration": min(opts.duration_seconds, 8) or 4,
            "resolution": "720p",
        }
        if opts.first_frame_path:
            body["images"] = [base64.b64encode(
                open(opts.first_frame_path, "rb").read()).decode()]
        resp = await self.http.post(
            f"{self.BASE}/img2video" if opts.first_frame_path else f"{self.BASE}/text2video",
            json=body, headers={"Authorization": f"Token {self.cfg.api_key}"})
        data = resp.json()
        return VideoTask(provider_task_id=data["task_id"], status="submitted")

    async def poll(self, task: VideoTask) -> VideoTask:
        resp = await self.http.get(
            f"https://api.vidu.com/ent/v2/tasks/{task.provider_task_id}",
            headers={"Authorization": f"Token {self.cfg.api_key}"})
        data = resp.json()
        return VideoTask(
            provider_task_id=task.provider_task_id,
            status={"success": "done", "failed": "failed"}.get(data["state"], "rendering"),
            video_url=data.get("video_url", ""), error=data.get("err", ""))


# ---------------------------------------------------------------- minimax (hailuo)
class MiniMaxProvider(VideoProvider):
    name = "minimax"
    BASE = "https://api.minimax.chat/v1"

    async def submit(self, opts: SubmitOptions) -> VideoTask:
        body = {"model": self.cfg.model or "video-01", "prompt": opts.prompt}
        if opts.first_frame_path:
            body["first_frame_image"] = base64.b64encode(
                open(opts.first_frame_path, "rb").read()).decode()
        resp = await self.http.post(
            f"{self.BASE}/video_generation", json=body,
            headers={"Authorization": f"Bearer {self.cfg.api_key}"})
        data = resp.json()
        return VideoTask(provider_task_id=data["task_id"], status="submitted")

    async def poll(self, task: VideoTask) -> VideoTask:
        resp = await self.http.get(
            f"{self.BASE}/query/video_generation?task_id={task.provider_task_id}",
            headers={"Authorization": f"Bearer {self.cfg.api_key}"})
        data = resp.json()
        return VideoTask(
            provider_task_id=task.provider_task_id,
            status={"Success": "done", "Fail": "failed"}.get(data["status"], "rendering"),
            video_url=data.get("file", ""), error=data.get("message", ""))


# ---------------------------------------------------------------- jimeng
class JimengProvider(VideoProvider):
    """Jimeng (即梦) via Volcengine Ark video endpoint (reference implementation)."""
    name = "jimeng"
    BASE = "https://ark.cn-beijing.volces.com/api/v3"

    async def submit(self, opts: SubmitOptions) -> VideoTask:
        content: list[dict] = [{"type": "text", "text": opts.prompt}]
        if opts.first_frame_path:
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(
                    open(opts.first_frame_path, "rb").read()).decode()}})
        body = {"model": self.cfg.model or "doubao-seedance-1-0-lite",
                "content": content}
        resp = await self.http.post(
            f"{self.BASE}/contents/generations/tasks", json=body,
            headers={"Authorization": f"Bearer {self.cfg.api_key}"})
        data = resp.json()
        return VideoTask(provider_task_id=data["id"], status="submitted")

    async def poll(self, task: VideoTask) -> VideoTask:
        resp = await self.http.get(
            f"{self.BASE}/contents/generations/tasks/{task.provider_task_id}",
            headers={"Authorization": f"Bearer {self.cfg.api_key}"})
        data = resp.json()
        status = data.get("status", "running")
        url = ""
        for item in data.get("content", {}).get("video_url", []) or []:
            url = item.get("video_url", "")
        return VideoTask(provider_task_id=task.provider_task_id,
                         status={"succeeded": "done", "failed": "failed"}.get(status, "rendering"),
                         video_url=url, error=data.get("error", ""))


PROVIDERS: dict[str, type[VideoProvider]] = {
    KlingProvider.name: KlingProvider,
    ViduProvider.name: ViduProvider,
    MiniMaxProvider.name: MiniMaxProvider,
    JimengProvider.name: JimengProvider,
}


def get_provider(cfg: ModelConfig, http: httpx.AsyncClient | None = None) -> VideoProvider:
    if cfg.provider not in PROVIDERS:
        raise KeyError(f"unknown video provider: {cfg.provider!r}; known: {list(PROVIDERS)}")
    return PROVIDERS[cfg.provider](cfg, http)
