"""End-to-end pipeline test with fully mocked models (mock:// base urls).

Covers: all 6 stages in order, video polling, export, prompt assembly logging,
immutable run history, parent-chain staleness, and manual adoption.
"""
import json

import pytest

from app.config import AppConfig, ModelConfig
from app.exporter import Exporter
from app.project_store import ProjectStore
from app.stage_engine import StageEngine
from app import stages as S


def mock_config() -> AppConfig:
    return AppConfig(models={
        "brain": ModelConfig(role="brain", base_url="mock://local", model="mock-brain"),
        "text": ModelConfig(role="text", base_url="mock://local", model="mock-text"),
        "image": ModelConfig(role="image", base_url="mock://local", model="mock-image"),
        "video": ModelConfig(role="video", provider="mock", base_url="mock://local",
                             model="mock-video"),
    }, global_prompt="全局风格提示")


@pytest.fixture()
def env(tmp_path):
    store = ProjectStore(tmp_path / "projects")
    engine = StageEngine(store, mock_config(), exporter=Exporter())
    return store, engine


async def test_full_pipeline_all_stages(env):
    store, engine = env
    pid = store.create("e2e", [{"type": "text", "ref": "一只猫在弹钢琴"}])["id"]
    store.write_skill(pid, "电影感.md", "整体电影质感")
    store.update(pid, {"active_skills": ["电影感.md"], "project_prompt": "中文短视频"})

    # brain stages
    for stage in S.BRAIN_STAGES:
        r = await engine.run_stage(pid, stage)
        assert r["status"] == "done", r

    # first frames
    r = await engine.run_stage(pid, S.FIRST_FRAMES)
    assert r["status"] == "done"
    shots = r["output"]["shots"]
    assert len(shots) == 2 and all(s["ok"] for s in shots)
    for s in shots:
        p = store.run_dir(pid, S.FIRST_FRAMES, "run-0001") / s["image"]
        assert p.exists() and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # video generation + polling
    r = await engine.run_stage(pid, S.VIDEO_GEN)
    assert r["status"] == "done"
    run_id = r["run_id"]
    out = r["output"]
    assert out["provider"] == "mock" and len(out["shots"]) == 2

    p1 = await engine.poll_video_tasks(pid, run_id)
    assert p1["finished"] is False            # rendering on first poll
    p2 = await engine.poll_video_tasks(pid, run_id)
    assert p2["finished"] is True
    for s in p2["output"]["shots"]:
        f = store.run_dir(pid, S.VIDEO_GEN, run_id) / s["video_file"]
        assert s["status"] == "done" and f.exists()

    # export
    r = await engine.run_stage(pid, S.EXPORT)
    assert r["status"] == "done", r
    assert r["output"]["mode"] in ("ffmpeg", "materials")
    if r["output"]["mode"] == "ffmpeg":
        assert (store.run_dir(pid, S.EXPORT, "run-0001") / "final.mp4").exists()

    # prompt assembly + skill layer visible in intent run events
    events = [json.loads(l) for l in (store.run_dir(pid, S.INTENT, "run-0001")
                                      / "events.ndjson").read_text("utf-8").splitlines()]
    sp = next(e for e in events if e["type"] == "system_prompt_assembled")
    assert sp["layers"] == ["base", "global", "project", "skill[0]"]
    assert "全局风格提示" in sp["text"] and "电影质感" in sp["text"]


async def test_rerun_creates_new_immutable_run_and_stale_hint(env):
    store, engine = env
    pid = store.create("t")["id"]
    r1 = await engine.run_stage(pid, S.INTENT)
    r2 = await engine.run_stage(pid, S.STORYBOARD)
    assert r2["status"] == "done"

    # re-run intent -> storyboard becomes stale (parent != current), hint only
    r1b = await engine.run_stage(pid, S.INTENT)
    assert r1b["run_id"] == "run-0002"
    view = store.stage_view(pid)
    sb = view[S.STORYBOARD]["runs"][0]
    assert sb["stale"] is True and sb["status"] == "done"   # still usable
    # old intent run untouched
    old = json.loads((store.run_dir(pid, S.INTENT, r1["run_id"]) / "output.json")
                     .read_text("utf-8"))
    assert old["summary"]


async def test_manual_adoption_records_new_run(env):
    store, engine = env
    pid = store.create("t")["id"]
    r = await engine.run_stage(pid, S.INTENT)
    edited = {"summary": "用户手工修正", "style": "vlog"}
    new_id, run_dir = store.create_run(pid, S.INTENT, {})
    (run_dir / "output.json").write_text(json.dumps(edited, ensure_ascii=False), "utf-8")
    store.set_run_status(pid, S.INTENT, new_id, "done")
    assert new_id == "run-0002"
    out = json.loads((store.run_dir(pid, S.INTENT, new_id) / "output.json").read_text("utf-8"))
    assert out["summary"] == "用户手工修正"
    # original run untouched
    assert json.loads((store.run_dir(pid, S.INTENT, r["run_id"]) / "output.json")
                     .read_text("utf-8"))["summary"] != "用户手工修正"
