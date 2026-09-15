"""FastAPI server — REST + SSE, serves the built WebUI from ui/dist."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import stages as S
from .config import AppConfig, load_config, save_config, ModelConfig, DEFAULT_DIR
from .event_logger import read_events
from .exporter import Exporter
from .project_store import ProjectStore, ProjectError
from .stage_engine import StageEngine, StageEngineError


class ModelIn(BaseModel):
    role: str
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    extra: dict = {}


class ProjectIn(BaseModel):
    name: str
    inputs: list[dict] = []


class ProjectPatch(BaseModel):
    name: str | None = None
    project_prompt: str | None = None
    stage_overrides: dict[str, str] | None = None
    active_skills: list[str] | None = None
    video_provider: str | None = None
    inputs: list[dict] | None = None


class SkillIn(BaseModel):
    filename: str
    content: str


class RunIn(BaseModel):
    note: str = ""


class AdoptIn(BaseModel):
    output: dict


class ChatIn(BaseModel):
    message: str


PROJECTS_DIR = Path(DEFAULT_DIR) / "projects"


def build_app(config: AppConfig | None = None, projects_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="video-creater")
    app.state.config = config or load_config()
    app.state.store = ProjectStore(projects_dir or PROJECTS_DIR)
    app.state.tasks: dict[str, asyncio.Task] = {}
    app.state.task_events: dict[str, list] = {}   # run_key -> progress lines
    app.state.task_results: dict[str, dict] = {}
    app.state.lock = asyncio.Lock()

    def engine() -> StageEngine:
        return StageEngine(app.state.store, app.state.config,
                           exporter=Exporter())

    def run_key(pid: str, stage: str) -> str:
        return f"{pid}:{stage}"

    # ---------------- config ----------------
    @app.get("/api/config")
    def get_config():
        cfg = app.state.config
        return {"global_prompt": cfg.global_prompt,
                "models": {r: m.model_dump() for r, m in cfg.models.items()}}

    @app.put("/api/config")
    def put_config(global_prompt: str = "", models: dict[str, ModelIn] | None = None):
        cfg = app.state.config
        cfg.global_prompt = global_prompt
        if models:
            cfg.models = {r: ModelConfig(**m.model_dump()) for r, m in models.items()}
        save_config(cfg)
        return {"ok": True}

    @app.post("/api/config/test")
    async def test_model(role: str):
        eng = StageEngine(app.state.store, app.state.config)
        try:
            client = eng._client(role)
            if role == "brain":
                reply = await client.chat([{"role": "user", "content": "ping"}])
                return {"ok": True, "reply": reply[:100]}
            return {"ok": True, "note": "role has no cheap test; config saved"}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": str(ex)}

    # ---------------- projects ----------------
    @app.post("/api/projects")
    def create_project(body: ProjectIn):
        return app.state.store.create(body.name, body.inputs)
    @app.get("/api/projects")
    def list_projects():
        return app.state.store.list_projects()

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        try:
            return {"project": app.state.store.get(pid),
                    "stages": app.state.store.stage_view(pid),
                    "skills": app.state.store.list_skills(pid)}
        except ProjectError as ex:
            raise HTTPException(404, str(ex))

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: str, body: ProjectPatch):
        try:
            return app.state.store.update(pid, body.model_dump(exclude_none=True))
        except ProjectError as ex:
            raise HTTPException(400, str(ex))

    # ---------------- skills ----------------
    @app.post("/api/projects/{pid}/skills")
    def write_skill(pid: str, body: SkillIn):
        try:
            app.state.store.write_skill(pid, body.filename, body.content)
            return app.state.store.list_skills(pid)
        except ProjectError as ex:
            raise HTTPException(400, str(ex))

    # ---------------- runs ----------------
    @app.post("/api/projects/{pid}/stages/{stage}/runs")
    async def start_run(pid: str, stage: str, body: RunIn = None):
        body = body or RunIn()
        key = run_key(pid, stage)
        if key in app.state.tasks and not app.state.tasks[key].done():
            raise HTTPException(409, "a run is already in progress for this stage")

        async def _run():
            try:
                result = await engine().run_stage(pid, stage, body.note)
            except (StageEngineError, ProjectError) as ex:
                result = {"status": "failed", "error": str(ex)}
            app.state.task_results[key] = result

        async with app.state.lock:
            app.state.task_events.setdefault(key, []).clear()
            app.state.tasks[key] = asyncio.create_task(_run())
        return {"ok": True}

    @app.post("/api/projects/{pid}/stages/{stage}/runs/{run_id}/current")
    def set_current(pid: str, stage: str, run_id: str):
        try:
            return app.state.store.set_current_run(pid, stage, run_id)
        except ProjectError as ex:
            raise HTTPException(404, str(ex))

    @app.post("/api/projects/{pid}/stages/{stage}/runs/{run_id}/adopt_")
    def adopt_run(pid: str, stage: str, run_id: str, body: AdoptIn):
        """Manual adoption: record user-edited output as a NEW run (immutable history)."""
        try:
            parents = {up: app.state.store.get(pid)["stages"][up]["current_run"]
                       for up in S.STAGES[:S.STAGES.index(stage)]
                       if app.state.store.get(pid)["stages"][up]["current_run"]}
            new_id, run_dir = app.state.store.create_run(pid, stage, parents)
            (run_dir / "output.json").write_text(
                json.dumps(body.output, ensure_ascii=False, indent=2), "utf-8")
            app.state.store.set_run_status(pid, stage, new_id, "done")
            return {"run_id": new_id}
        except ProjectError as ex:
            raise HTTPException(400, str(ex))

    @app.get("/api/projects/{pid}/stages/{stage}/runs/{run_id}")
    def get_run(pid: str, stage: str, run_id: str):
        try:
            run_dir = app.state.store.run_dir(pid, stage, run_id)
            output = None
            f = run_dir / "output.json"
            if f.exists():
                output = json.loads(f.read_text("utf-8"))
            return {"run": app.state.store.get_run(pid, stage, run_id),
                    "output": output,
                    "events": read_events(run_dir),
                    "files": sorted(p.name for p in run_dir.iterdir() if p.is_file())}
        except ProjectError as ex:
            raise HTTPException(404, str(ex))

    @app.get("/api/projects/{pid}/stages/{stage}/runs/{run_id}/files/{fname}")
    def get_artifact(pid: str, stage: str, run_id: str, fname: str):
        try:
            p = app.state.store.artifact(pid, stage, run_id, fname)
        except ProjectError as ex:
            raise HTTPException(404, str(ex))
        if not p.exists() or p.is_dir():
            raise HTTPException(404, "file not found")
        return FileResponse(p)

    # ---------------- initial materials (images / existing video) ----------------
    @app.post("/api/projects/{pid}/inputs")
    async def upload_input(pid: str, file: UploadFile):
        """Initial material upload: image or existing video, stored under inputs/."""
        try:
            state = app.state.store.get(pid)
            inputs_dir = app.state.store._pdir(pid) / "inputs"
        except ProjectError as ex:
            raise HTTPException(404, str(ex))
        if file.filename is None:
            raise HTTPException(400, "missing filename")
        fname = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", file.filename)
        kind = "video" if (file.content_type or "").startswith("video") else "image"
        data = await file.read()
        if len(data) > 200 * 1024 * 1024:
            raise HTTPException(413, "file too large (max 200MB)")
        (inputs_dir / fname).write_bytes(data)
        state["inputs"].append({"type": kind, "ref": f"inputs/{fname}", "name": fname})
        app.state.store._save(pid, state)
        return {"ok": True, "inputs": state["inputs"]}

    @app.get("/api/projects/{pid}/inputs/{fname}")
    def get_input(pid: str, fname: str):
        try:
            p = app.state.store._pdir(pid) / "inputs" / Path(fname).name
        except ProjectError as ex:
            raise HTTPException(404, str(ex))
        if not p.exists():
            raise HTTPException(404, "file not found")
        return FileResponse(p)

    @app.post("/api/projects/{pid}/poll_video/{run_id}")
    async def poll_video(pid: str, run_id: str):
        try:
            eng = engine()
            return await eng.poll_video_tasks(pid, run_id)
        except (StageEngineError, ProjectError, KeyError) as ex:
            raise HTTPException(400, str(ex))

    # ---------------- conversational material prep ----------------
    from .chat import ChatEngine

    def chat_engine() -> ChatEngine:
        return ChatEngine(app.state.store, app.state.config,
                          http_factory=None)

    @app.get("/api/projects/{pid}/chat")
    def get_chat(pid: str):
        try:
            eng = chat_engine()
            return {"history": eng.history(pid),
                    "readiness": eng.readiness(pid),
                    "materials": [{"name": m.get("name") or (m.get("ref") or "")[:40],
                                   "type": m.get("type")} for m in eng.materials(pid)],
                    "skills": [{"name": s["filename"]} for s in eng.skills(pid)]}
        except ProjectError as ex:
            raise HTTPException(404, str(ex))

    @app.post("/api/projects/{pid}/chat")
    async def post_chat(pid: str, body: ChatIn):
        try:
            eng = chat_engine()
            return await eng.chat(pid, body.message)
        except ProjectError as ex:
            raise HTTPException(404, str(ex))

    # ---------------- SSE ----------------
    @app.get("/api/projects/{pid}/events")
    async def sse(pid: str):
        from sse_starlette.sse import EventSourceResponse

        async def gen():
            seen: dict[str, str] = {}
            while True:
                view = app.state.store.stage_view(pid)
                summary = json.dumps(view, ensure_ascii=False, default=str)
                if summary != seen.get("v"):
                    seen["v"] = summary
                    yield {"event": "stages", "data": summary}
                await asyncio.sleep(1.5)

        return EventSourceResponse(gen())

    # ---------------- static UI ----------------
    ui_dist = Path(__file__).resolve().parent.parent / "ui" / "dist"
    if ui_dist.exists():
        app.mount("/assets", StaticFiles(directory=ui_dist / "assets"), name="assets")

        @app.get("/")
        def index():
            return FileResponse(ui_dist / "index.html")

    return app


app = build_app()


def main():  # pragma: no cover
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8642)


if __name__ == "__main__":  # pragma: no cover
    main()
