"""ChatEngine — conversational material preparation.

A free-form (non-JSON) chat with the brain whose job is to guide the user
through preparing materials: references via @file / $skill, stage status in
context, and a readiness checklist that says when video generation can start.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import stages as S
from .model_client import ModelClient
from .project_store import ProjectStore, ProjectError
from .prompt_composer import PromptComposer, PromptLayers

READY_MARK = "[READY_FOR_VIDEO]"

CHAT_SYSTEM_BASE = f"""\
You are the creative assistant of a video-creation pipeline. Your job in this
chat is to help the user PREPARE materials step by step: character references,
scene references, a storyboard script, and first-frame images. Ask focused
questions, summarize decisions, and suggest concrete next actions (e.g. upload
a reference image, refine a shot description, run a stage). Reply in the user's
language, concisely. When — and only when — the storyboard exists AND every
shot has a first-frame image (or the user explicitly chose text-to-video
without frames), end your reply with the exact marker {READY_MARK} on its own
line. Otherwise never output that marker.
"""


class ChatEngine:
    def __init__(self, store: ProjectStore, config, http_factory=None):
        self.store = store
        self.config = config
        self.http_factory = http_factory
        self.composer = PromptComposer()

    # ---- history ----
    def _chat_path(self, pid: str) -> Path:
        return self.store._pdir(pid) / "chat.json"

    def history(self, pid: str) -> list[dict]:
        p = self._chat_path(pid)
        if not p.exists():
            return []
        return json.loads(p.read_text("utf-8"))

    def _append(self, pid: str, role: str, content: str, refs: list | None = None) -> dict:
        msg = {"role": role, "content": content, "refs": refs or []}
        h = self.history(pid)
        h.append(msg)
        self._chat_path(pid).write_text(json.dumps(h, ensure_ascii=False, indent=2), "utf-8")
        return msg

    # ---- references ----
    def materials(self, pid: str) -> list[dict]:
        """All referenceable materials: initial inputs + uploaded files."""
        state = self.store.get(pid)
        return [i for i in state.get("inputs", []) if i.get("ref") or i.get("name")]

    def skills(self, pid: str) -> list[dict]:
        return self.store.list_skills(pid)

    def _resolve_refs(self, pid: str, text: str) -> tuple[str, list[dict]]:
        """Expand @name / $name references into attached context blocks.

        Returns (expanded_text, resolved_refs). Unknown references are kept
        verbatim so the user sees they didn't resolve.
        """
        resolved: list[dict] = []
        blocks: list[str] = []

        def sub_file(m: re.Match) -> str:
            raw = m.group(1)                      # @文件名 或 @文件名 尾部
            mats = self.materials(pid)
            hit = None
            for i in reversed(mats):              # prefer latest, match by name/ref tail
                key = i.get("name") or i.get("ref", "")
                if raw and (key == raw or key.endswith(raw) or raw.endswith(key)):
                    hit = i
                    break
            if hit is None:
                for i in mats:
                    if raw and raw in (i.get("name") or i.get("ref") or ""):
                        hit = i
                        break
            if hit is None:
                return m.group(0)
            resolved.append({"kind": "material", "name": hit.get("name") or hit.get("ref"),
                             "type": hit.get("type")})
            if hit.get("type") == "text":
                blocks.append(f"[referenced material {hit.get('ref')}]")
            else:
                blocks.append(f"[referenced {hit.get('type')} file: {hit.get('name')}]")
            return ""

        def sub_skill(m: re.Match) -> str:
            raw = m.group(1).removesuffix(".md")
            for s in self.skills(pid):
                if s["filename"].removesuffix(".md") == raw or s["filename"] == m.group(1):
                    resolved.append({"kind": "skill", "name": s["filename"]})
                    blocks.append(f"[activated skill for this message]\n{s['content'][:2000]}")
                    return ""
            return m.group(0)

        out = re.sub(r"@([\w.\-\u4e00-\u9fff]+)", sub_file, text)
        out = re.sub(r"\$([\w.\-\u4e00-\u9fff]+)", sub_skill, out)
        if blocks:
            out = out.rstrip() + "\n\n" + "\n\n".join(blocks)
        return out, resolved

    # ---- readiness ----
    def readiness(self, pid: str) -> dict:
        """Checklist of materials required before video generation."""
        state = self.store.get(pid)
        mats = self.materials(pid)
        images = [m for m in mats if m.get("type") == "image"]

        def stage_state(stage: str) -> dict:
            s = state["stages"][stage]
            cur = s["current_run"]
            status = "empty"
            if cur:
                r = next((r for r in s["runs"] if r["id"] == cur), None)
                status = (r or {}).get("status", "empty")
            return {"ready": status == "done", "current_run": cur, "status": status}

        storyboard = stage_state(S.STORYBOARD)
        shot_count = 0
        frames_ready = False
        missing_frames: list[int] = []
        if storyboard["ready"]:
            sb_dir = self.store.run_dir(pid, S.STORYBOARD, storyboard["current_run"])
            f = sb_dir / "output.json"
            if f.exists():
                board = json.loads(f.read_text("utf-8"))
                shot_count = len(board.get("shots", []))
        ff = stage_state(S.FIRST_FRAMES)
        if storyboard["ready"] and shot_count:
            need = {i + 1 for i in range(shot_count)}
            out = None
            if ff["ready"]:
                f = (self.store.run_dir(pid, S.FIRST_FRAMES, ff["current_run"])
                     / "output.json")
                if f.exists():
                    out = json.loads(f.read_text("utf-8"))
            if out is None:
                missing_frames = sorted(need)
            else:
                ok_shots = {s["index"] for s in out.get("shots", []) if s.get("ok")}
                missing_frames = sorted(need - ok_shots)
            frames_ready = not missing_frames
        video_ready = storyboard["ready"] and (frames_ready or ff["status"] == "skipped")
        return {
            "materials": {"count": len(mats), "images": len(images),
                          "items": [{"name": m.get("name") or m.get("ref", "")[:40],
                                     "type": m.get("type")} for m in mats]},
            "storyboard": {**storyboard, "shots": shot_count},
            "first_frames": {**ff, "missing": missing_frames},
            "video_gen": {"ready": video_ready,
                          "reason": "" if video_ready else
                          ("" if not storyboard["ready"] else
                           f"missing first frames for shots {missing_frames}")},
        }

    # ---- context for the brain ----
    def _context_summary(self, pid: str) -> str:
        state = self.store.get(pid)
        r = self.readiness(pid)
        lines = [
            f"Project: {state['name']}",
            f"Stage status:",
            f"- intent: {state['stages'][S.INTENT]['current_run'] or 'none'}",
            f"- storyboard: {r['storyboard']['status']} ({r['storyboard']['shots']} shots)",
            f"- shot_prompts: {state['stages'][S.SHOT_PROMPTS]['current_run'] or 'none'}",
            f"- first_frames: {r['first_frames']['status']}"
            + (f" (missing shots {r['first_frames']['missing']})" if r['first_frames']['missing'] else ""),
            f"- video_gen: {state['stages'][S.VIDEO_GEN]['current_run'] or 'none'}",
            f"Materials: {r['materials']['count']} ({r['materials']['images']} images)",
        ]
        return "\n".join(lines)

    async def chat(self, pid: str, user_text: str) -> dict:
        expanded, refs = self._resolve_refs(pid, user_text)
        self._append(pid, "user", user_text, refs)
        history = self.history(pid)

        state = self.store.get(pid)
        skills = [sk["content"] for sk in self.store.list_skills(pid)
                  if sk["filename"] in state.get("active_skills", [])]
        # one-shot skills referenced via $ are attached to that message already
        layers = PromptLayers(
            global_prompt=self.config.global_prompt,
            project_prompt=state.get("project_prompt", ""),
            skills=skills,
        )
        composed = self.composer.compose(layers)
        system = (f"{CHAT_SYSTEM_BASE}\n\n# --- project context ---\n"
                  f"{self._context_summary(pid)}\n\n# --- user prompt layers ---\n{composed.text}")

        cfg = self.config.model_for("brain")
        if cfg is None:
            reply = "（未配置 Agent 大脑模型，请在设置页填写后重试）"
            self._append(pid, "assistant", reply)
            return {"reply": reply, "readiness": self.readiness(pid)}

        client = ModelClient(cfg, http=self.http_factory() if self.http_factory else None)
        messages = [{"role": "system", "content": system}] + [
            {"role": m["role"], "content": m["content"]} for m in history[-20:]]
        try:
            reply = await client.chat(messages)
        except Exception as ex:  # noqa: BLE001 — chat must not dead-end
            reply = f"（大脑调用失败：{ex}）"
        ready_hint = READY_MARK in reply
        reply = reply.replace(READY_MARK, "").strip()
        self._append(pid, "assistant", reply)
        return {"reply": reply, "ready_hint": ready_hint,
                "readiness": self.readiness(pid)}
