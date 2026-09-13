"""Fixed pipeline skeleton (v1: no dynamic branching by input type)."""
from __future__ import annotations

INTENT = "intent"
STORYBOARD = "storyboard"
SHOT_PROMPTS = "shot_prompts"
FIRST_FRAMES = "first_frames"
VIDEO_GEN = "video_gen"
EXPORT = "export"

STAGES: list[str] = [INTENT, STORYBOARD, SHOT_PROMPTS, FIRST_FRAMES, VIDEO_GEN, EXPORT]

# Stages where the agent brain is invoked (prompt overrides apply here only).
BRAIN_STAGES: list[str] = [INTENT, STORYBOARD, SHOT_PROMPTS]

STAGE_TITLES = {
    INTENT: "意图理解",
    STORYBOARD: "分镜/脚本",
    SHOT_PROMPTS: "逐镜头 Prompt",
    FIRST_FRAMES: "首帧图生成",
    VIDEO_GEN: "视频生成",
    EXPORT: "拼接导出",
}

STAGE_INSTRUCTIONS: dict[str, str] = {
    INTENT: (
        "Understand the user's initial materials (text / images / video) and goal. "
        'Respond as JSON: {"summary": str, "style": str, "audience": str, "notes": str, '
        '"shot_count_hint": int}'
    ),
    STORYBOARD: (
        "Produce a numbered storyboard/script from the intent output and materials. "
        'Respond as JSON: {"shots": [{"index": int, "description": str, "narration": str, '
        '"duration_seconds": int}], "notes": str}'
    ),
    SHOT_PROMPTS: (
        "For each shot, write a video-generation prompt optimized for the target provider "
        '(subject, action, camera, lighting, style). Respond as JSON: {"shots": [{"index": int, '
        '"video_prompt": str, "image_prompt": str}], "notes": str}'
    ),
}
