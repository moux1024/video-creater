"""Built-in mock models — let the full pipeline run without real keys.

Configure any role with base_url="mock://local" (and optionally provider="mock"
for the video role) to get deterministic canned behavior:

- brain chat: returns stage-appropriate JSON (detected from the user prompt)
- image generation: writes a tiny PNG
- video provider: submit → poll(rendering) → poll(done) → download(fake mp4)
"""
from __future__ import annotations

import base64
import struct
import zlib

MOCK_BASE = "mock://local"

# 1x1 transparent PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhf"
    "DwAChwGA60e6kgAAAABJRU5ErkJggg==")

MOCK_MP4 = b"\x00\x00\x00\x18ftypmp42MOCKVIDEO" + b"\x00" * 64


def png_bytes(w: int = 8, h: int = 8, rgb: tuple = (66, 133, 244)) -> bytes:
    """Generate a minimal solid-color PNG."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def mock_brain_response(user_prompt: str) -> str:
    """Canned JSON matching the stage schema inferred from the user prompt."""
    import json
    head = user_prompt[:60]
    if head.startswith("Initial materials"):
        obj = {"summary": "一只猫弹钢琴的趣味短片", "style": "电影感",
               "audience": "短视频观众", "notes": "mock 意图", "shot_count_hint": 2}
    elif head.startswith("Storyboard:"):
        obj = {"shots": [
            {"index": i, "description": f"mock 镜头 {i}", "narration": "",
             "duration_seconds": 3} for i in (1, 2)],
            "notes": "mock 分镜"}
    else:  # shot prompts
        obj = {"shots": [
            {"index": i, "video_prompt": f"mock video prompt {i}",
             "image_prompt": f"mock image prompt {i}"} for i in (1, 2)],
            "notes": "mock 逐镜头"}
    return json.dumps(obj, ensure_ascii=False)


def is_mock(base_url: str) -> bool:
    return base_url.startswith("mock://")
