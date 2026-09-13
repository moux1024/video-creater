"""StageEngine — fixed pipeline skeleton, append-only runs, parent chains.

Brain stages (intent / storyboard / shot_prompts) call the LLM with the composed
prompt; material stages (first_frames / video_gen / export) call image / video /
ffmpeg. Every run is immutable; parents record which upstream runs were consumed.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import stages as S
from .config import AppConfig
from .event_logger import EventLogger
from .model_client import ModelClient
from .project_store import ProjectStore
from .prompt_composer import PromptComposer, PromptLayers


class StageEngineError(Exception):
    pass


class StageEngine:
    def __init__(self, store: ProjectStore, config: AppConfig,
                 http_factory=None, exporter=None):
        self.store = store
        self.config = config
        self.composer = PromptComposer()
        self._http_factory = http_factory      # () -> httpx.AsyncClient (injectable for tests)
        self._exporter = exporter

    # ---- helpers ----
    def _client(self, role: str) -> ModelClient:
        cfg = self.config.model_for(role)
        if cfg is None:
            raise StageEngineError(f"model role not configured: {role}")
        return ModelClient(cfg, http=self._http_factory() if self._http_factory else None)

    def _parents(self, project_id: str, stage: str) -> dict[str, str]:
        state = self.store.get(project_id)
        idx = S.STAGES.index(stage)
        parents = {}
        for up in S.STAGES[:idx]:
            cur = state["stages"][up]["current_run"]
            if cur:
                parents[up] = cur
        return parents

    def _current_output(self, project_id: str, stage: str) -> dict | None:
        state = self.store.get(project_id)
        cur = state["stages"][stage]["current_run"]
        if not cur:
            return None
        f = self.store.run_dir(project_id, stage, cur) / "output.json"
        if not f.exists():
            return None
        return json.loads(f.read_text("utf-8"))

    def _compose_system_prompt(self, project_id: str, stage: str):
        state = self.store.get(project_id)
        skills = [sk["content"] for sk in self.store.list_skills(project_id)
                  if sk["active"]]
        layers = PromptLayers(
            global_prompt=self.config.global_prompt,
            project_prompt=state.get("project_prompt", ""),
            skills=skills,
            stage_override=state.get("stage_overrides", {}).get(stage, ""),
        )
        composed = self.composer.compose(layers)
        return composed, self.composer.estimate_tokens(layers)

    # ---- stage execution ----
    async def run_stage(self, project_id: str, stage: str,
                        user_note: str = "") -> dict:
        if stage not in S.STAGES:
            raise StageEngineError(f"unknown stage: {stage}")
        parents = self._parents(project_id, stage)
        run_id, run_dir = self.store.create_run(project_id, stage, parents)
        log = EventLogger(run_dir)
        self.store.set_run_status(project_id, stage, run_id, "running")
        log.emit("run_started", stage=stage, run_id=run_id, parents=parents)
        try:
            result = await self._execute(project_id, stage, run_dir, log, user_note)
            (run_dir / "output.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
            self.store.set_run_status(project_id, stage, run_id, "done")
            log.emit("run_finished", stage=stage, run_id=run_id)
            return {"run_id": run_id, "status": "done", "output": result}
        except Exception as ex:  # noqa: BLE001 — surfaced to UI as failed run
            self.store.set_run_status(project_id, stage, run_id, "failed")
            log.emit("run_failed", stage=stage, run_id=run_id, error=str(ex))
            return {"run_id": run_id, "status": "failed", "error": str(ex)}

    async def _execute(self, project_id: str, stage: str, run_dir: Path,
                       log: EventLogger, user_note: str) -> dict:
        if stage in S.BRAIN_STAGES:
            return await self._run_brain_stage(project_id, stage, run_dir, log, user_note)
        if stage == S.FIRST_FRAMES:
            return await self._run_first_frames(project_id, stage, run_dir, log)
        if stage == S.VIDEO_GEN:
            return await self._run_video_gen(project_id, stage, run_dir, log)
        if stage == S.EXPORT:
            return self._run_export(project_id, run_dir, log)
        raise StageEngineError(f"unhandled stage: {stage}")

    # ---- brain stages ----
    async def _run_brain_stage(self, project_id: str, stage: str, run_dir: Path,
                               log: EventLogger, user_note: str) -> dict:
        composed, est = self._compose_system_prompt(project_id, stage)
        log.emit("system_prompt_assembled", text=composed.text,
                 layers=composed.provenance, est_tokens=est)

        state = self.store.get(project_id)
        if stage == S.INTENT:
            materials = json.dumps(state["inputs"], ensure_ascii=False)
            user_prompt = (f"Initial materials: {materials}\n"
                           f"Stage task: {S.STAGE_INSTRUCTIONS[stage]}")
        elif stage == S.STORYBOARD:
            intent = self._current_output(project_id, S.INTENT)
            if intent is None:
                raise StageEngineError("no current intent output; run intent stage first")
            user_prompt = (f"Intent: {json.dumps(intent, ensure_ascii=False)}\n"
                           f"Stage task: {S.STAGE_INSTRUCTIONS[stage]}")
        else:  # shot_prompts
            board = self._current_output(project_id, S.STORYBOARD)
            if board is None:
                raise StageEngineError("no current storyboard; run storyboard stage first")
            user_prompt = (f"Storyboard: {json.dumps(board, ensure_ascii=False)}\n"
                           f"Stage task: {S.STAGE_INSTRUCTIONS[stage]}")
        if user_note:
            user_prompt += f"\nUser note: {user_note}"

        log.emit("brain_request", stage=stage, user_prompt=user_prompt)
        client = self._client("brain")
        result = await client.chat_json(composed.text, user_prompt)
        log.emit("brain_response", ok=result.ok, attempts=result.attempts,
                 raw=result.raw, error=result.error)
        if not result.ok:
            (run_dir / "raw_output.txt").write_text(result.raw, "utf-8")
            raise StageEngineError(
                f"brain JSON invalid after {result.attempts} attempts: {result.error}; "
                f"raw output saved for manual adoption")
        return result.data

    # ---- first frames ----
    async def _run_first_frames(self, project_id: str, stage: str,
                                run_dir: Path, log: EventLogger) -> dict:
        prompts_out = self._current_output(project_id, S.SHOT_PROMPTS)
        if prompts_out is None:
            raise StageEngineError("no current shot prompts; run shot_prompts stage first")
        cfg = self.config.model_for("image")
        if cfg is None:
            raise StageEngineError("image role not configured; skipping is allowed by "
                                   "going straight to video_gen")
        client = self._client("image")
        results = []
        for shot in prompts_out.get("shots", []):
            idx = shot.get("index")
            log.emit("image_request", shot=idx)
            path = run_dir / f"shot-{idx:02d}.png"
            try:
                await client.generate_image(shot.get("image_prompt", ""), path)
                log.emit("image_done", shot=idx, path=path.name)
                results.append({"index": idx, "image": path.name, "ok": True})
            except Exception as ex:  # noqa: BLE001
                log.emit("image_failed", shot=idx, error=str(ex))
                results.append({"index": idx, "image": "", "ok": False, "error": str(ex)})
        return {"shots": results}

    # ---- video generation ----
    async def _run_video_gen(self, project_id: str, stage: str,
                             run_dir: Path, log: EventLogger) -> dict:
        prompts_out = self._current_output(project_id, S.SHOT_PROMPTS)
        if prompts_out is None:
            raise StageEngineError("no current shot prompts; run shot_prompts stage first")
        state = self.store.get(project_id)
        cfg = self.config.model_for("video")
        if cfg is None:
            raise StageEngineError("video role not configured")
        if cfg.provider != state.get("video_provider") and state.get("video_provider"):
            cfg = cfg.model_copy(update={"provider": state["video_provider"]})
        from .video_provider import get_provider, SubmitOptions
        provider = get_provider(cfg, self._http_factory() if self._http_factory else None)

        frames = self._current_output(project_id, S.FIRST_FRAMES) or {"shots": []}
        frame_by_idx = {s["index"]: s.get("image", "")
                        for s in frames.get("shots", []) if s.get("ok") and s.get("image")}

        results = []
        for shot in prompts_out.get("shots", []):
            idx = shot.get("index")
            first_frame = ""
            if idx in frame_by_idx:
                first_frame = str(self.store.run_dir(
                    project_id, S.FIRST_FRAMES,
                    self.store.get(project_id)["stages"][S.FIRST_FRAMES]["current_run"])
                    / frame_by_idx[idx])
            opts = SubmitOptions(prompt=shot.get("video_prompt", ""),
                                 first_frame_path=first_frame)
            log.emit("video_submit", shot=idx, mode="i2v" if first_frame else "t2v")
            task = await provider.submit(opts)
            log.emit("video_submitted", shot=idx, provider_task_id=task.provider_task_id)
            results.append({"index": idx, "provider_task_id": task.provider_task_id,
                            "status": "submitted"})
        return {"shots": results, "provider": cfg.provider,
                "note": "polling handled by poll_video_tasks; task ids logged for recovery"}

    async def poll_video_tasks(self, project_id: str, run_id: str) -> dict:
        """Poll provider tasks of a video_gen run; download finished videos."""
        out = self._current_output_video(project_id, run_id)
        state = self.store.get(project_id)
        cfg = self.config.model_for("video")
        from .video_provider import get_provider
        provider = get_provider(cfg, self._http_factory() if self._http_factory else None)
        run_dir = self.store.run_dir(project_id, S.VIDEO_GEN, run_id)
        log = EventLogger(run_dir)
        finished = True
        for shot in out.get("shots", []):
            if shot.get("status") in ("done", "failed"):
                continue
            from .video_provider import VideoTask
            task = await provider.poll(VideoTask(shot["provider_task_id"], "rendering"))
            log.emit("video_poll", shot=shot["index"], status=task.status,
                     provider_task_id=task.provider_task_id)
            shot["status"] = task.status
            shot["video_url"] = task.video_url
            shot["error"] = task.error
            if task.status == "done" and task.video_url and not shot.get("video_file"):
                fname = f"shot-{shot['index']:02d}.mp4"
                await provider.download(task, run_dir / fname)
                shot["video_file"] = fname
                log.emit("video_downloaded", shot=shot["index"], file=fname)
            if task.status not in ("done", "failed"):
                finished = False
        (run_dir / "output.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                             "utf-8")
        return {"finished": finished, "output": out}

    def _current_output_video(self, project_id: str, run_id: str) -> dict:
        f = self.store.run_dir(project_id, S.VIDEO_GEN, run_id) / "output.json"
        return json.loads(f.read_text("utf-8"))

    # ---- export ----
    def _run_export(self, project_id: str, run_dir: Path, log: EventLogger) -> dict:
        if self._exporter is None:
            raise StageEngineError("exporter not configured")
        video_out = self._current_output(project_id, S.VIDEO_GEN)
        if video_out is None:
            raise StageEngineError("no current video_gen output")
        state = self.store.get(project_id)
        video_run = state["stages"][S.VIDEO_GEN]["current_run"]
        video_dir = self.store.run_dir(project_id, S.VIDEO_GEN, video_run)
        shots = [s for s in video_out.get("shots", []) if s.get("video_file")]
        if not shots:
            raise StageEngineError("no downloaded shot videos; poll video tasks first")
        files = [str(video_dir / s["video_file"]) for s in sorted(shots, key=lambda x: x["index"])]
        result = self._exporter.export(files, run_dir, log)
        return result
