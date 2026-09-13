"""ProjectStore — files are the database.

Layout per project:
  <projects_dir>/<project_id>/
    project.json        state: prompts, skills, inputs, per-stage runs & current pointers
    skills/             user markdown skills
    inputs/             user-provided initial materials
    <stage>/run-NNNN/   immutable run artifacts + events.ndjson + log.txt
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from . import stages as S

RUN_STATUS = ("pending", "running", "done", "failed")


class ProjectError(Exception):
    pass


class ProjectStore:
    def __init__(self, projects_dir: Path):
        self.projects_dir = Path(projects_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    # ---- projects ----
    def _pdir(self, project_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id):
            raise ProjectError("invalid project id")
        d = self.projects_dir / project_id
        if not d.exists():
            raise ProjectError(f"project not found: {project_id}")
        return d

    def create(self, name: str, inputs: list[dict] | None = None) -> dict:
        pid = uuid.uuid4().hex[:12]
        d = self.projects_dir / pid
        d.mkdir(parents=True)
        state = {
            "id": pid,
            "name": name,
            "created_at": time.time(),
            "project_prompt": "",
            "stage_overrides": {},          # brain stages only
            "active_skills": [],            # filenames in skills/
            "inputs": inputs or [],         # [{"type": "text"|"image"|"video", "ref": str}]
            "video_provider": "",
            "stages": {st: {"current_run": None, "runs": []} for st in S.STAGES},
        }
        (d / "project.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        (d / "skills").mkdir()
        (d / "inputs").mkdir()
        return state

    def list_projects(self) -> list[dict]:
        out = []
        for d in sorted(self.projects_dir.iterdir()):
            f = d / "project.json"
            if f.exists():
                out.append(json.loads(f.read_text("utf-8")))
        return out

    def get(self, project_id: str) -> dict:
        return json.loads((self._pdir(project_id) / "project.json").read_text("utf-8"))

    def update(self, project_id: str, patch: dict) -> dict:
        state = self.get(project_id)
        allowed = {"name", "project_prompt", "stage_overrides", "active_skills",
                   "inputs", "video_provider"}
        for k, v in patch.items():
            if k in allowed:
                state[k] = v
        if "stage_overrides" in patch:
            bad = set(patch["stage_overrides"]) - set(S.BRAIN_STAGES)
            if bad:
                raise ProjectError(f"prompt overrides only allowed on brain stages: {bad}")
        self._save(project_id, state)
        return state

    def _save(self, project_id: str, state: dict) -> None:
        (self._pdir(project_id) / "project.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), "utf-8")

    # ---- runs (append-only, immutable) ----
    def create_run(self, project_id: str, stage: str, parents: dict[str, str]) -> tuple[str, Path]:
        if stage not in S.STAGES:
            raise ProjectError(f"unknown stage: {stage}")
        state = self.get(project_id)
        s = state["stages"][stage]
        run_id = f"run-{len(s['runs']) + 1:04d}"
        run_dir = self._pdir(project_id) / stage / run_id
        run_dir.mkdir(parents=True)
        s["runs"].append({
            "id": run_id,
            "parents": parents,           # {upstream_stage: run_id}
            "status": "pending",
            "created_at": time.time(),
        })
        s["current_run"] = run_id
        self._save(project_id, state)
        return run_id, run_dir

    def set_run_status(self, project_id: str, stage: str, run_id: str, status: str) -> None:
        if status not in RUN_STATUS:
            raise ProjectError(f"invalid status: {status}")
        state = self.get(project_id)
        for r in state["stages"][stage]["runs"]:
            if r["id"] == run_id:
                r["status"] = status
                self._save(project_id, state)
                return
        raise ProjectError(f"run not found: {stage}/{run_id}")

    def run_dir(self, project_id: str, stage: str, run_id: str) -> Path:
        d = self._pdir(project_id) / stage / run_id
        if not d.exists():
            raise ProjectError(f"run not found: {stage}/{run_id}")
        return d

    def get_run(self, project_id: str, stage: str, run_id: str) -> dict:
        state = self.get(project_id)
        for r in state["stages"][stage]["runs"]:
            if r["id"] == run_id:
                return r
        raise ProjectError(f"run not found: {stage}/{run_id}")

    def set_current_run(self, project_id: str, stage: str, run_id: str) -> dict:
        self.get_run(project_id, stage, run_id)  # must exist
        state = self.get(project_id)
        state["stages"][stage]["current_run"] = run_id
        self._save(project_id, state)
        return state

    def artifact(self, project_id: str, stage: str, run_id: str, rel: str) -> Path:
        base = self.run_dir(project_id, stage, run_id).resolve()
        p = (base / rel).resolve()
        if not str(p).startswith(str(base)):
            raise ProjectError("path escape")
        return p

    # ---- skills ----
    def skill_dir(self, project_id: str) -> Path:
        d = self._pdir(project_id) / "skills"
        d.mkdir(exist_ok=True)
        return d

    def list_skills(self, project_id: str) -> list[dict]:
        out = []
        for f in sorted(self.skill_dir(project_id).glob("*.md")):
            text = f.read_text("utf-8")
            out.append({
                "filename": f.name,
                "active": f.name in self.get(project_id)["active_skills"],
                "chars": len(text),
                "content": text,
            })
        return out

    def write_skill(self, project_id: str, filename: str, content: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]+\.md", filename):
            raise ProjectError("skill filename must be *.md")
        (self.skill_dir(project_id) / filename).write_text(content, "utf-8")

    # ---- staleness (parent-chain based; hint only) ----
    def stage_view(self, project_id: str) -> dict:
        """Per-stage runs with a computed `stale` flag on each run."""
        state = self.get(project_id)
        view = {}
        for idx, stage in enumerate(S.STAGES):
            upstream_current = {
                up: state["stages"][up]["current_run"]
                for up in S.STAGES[:idx]
                if state["stages"][up]["current_run"]
            }
            runs = []
            for r in state["stages"][stage]["runs"]:
                # stale = any recorded parent disagrees with that upstream stage's current run
                stale = any(r["parents"].get(up) != cur
                            for up, cur in upstream_current.items() if up in r["parents"])
                runs.append({**r, "stale": stale,
                             "is_current": state["stages"][stage]["current_run"] == r["id"]})
            view[stage] = {
                "title": S.STAGE_TITLES[stage],
                "is_brain_stage": stage in S.BRAIN_STAGES,
                "current_run": state["stages"][stage]["current_run"],
                "runs": runs,
            }
        return view
