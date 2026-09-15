"""ChatEngine tests: @/$ reference resolution, readiness checklist, chat loop."""
import json

import pytest

from app.config import AppConfig, ModelConfig
from app.project_store import ProjectStore
from app.chat import ChatEngine
from app import stages as S
from app.stage_engine import StageEngine
from tests.test_e2e import mock_config


@pytest.fixture()
def env(tmp_path):
    store = ProjectStore(tmp_path / "projects")
    return store, ChatEngine(store, mock_config())


async def test_reference_resolution(env):
    store, eng = env
    pid = store.create("t", [
        {"type": "text", "ref": "主角是橘猫"},
        {"type": "image", "ref": "inputs/ref.png", "name": "ref.png"},
    ])["id"]
    store.write_skill(pid, "电影感.md", "16mm 胶片质感")

    out, refs = eng._resolve_refs(
        pid, "参考 @ref.png 和文字 @不存在.png，风格 $电影感，忽略 $不存在")
    assert ("@不存在.png" in out or "@不存在" in out) and "$不存在" in out
    assert "16mm 胶片质感" in out                      # skill content attached
    assert {"kind": "material", "name": "ref.png", "type": "image"} in refs
    assert {"kind": "skill", "name": "电影感.md"} in refs


def test_readiness_progression(env):
    store, eng = env
    pid = store.create("t", [{"type": "image", "ref": "inputs/a.png", "name": "a.png"}])["id"]

    r = eng.readiness(pid)
    assert r["materials"]["count"] == 1 and not r["storyboard"]["ready"]
    assert not r["video_gen"]["ready"]

    # simulate storyboard done with 2 shots
    rid, rd = store.create_run(pid, S.STORYBOARD, {})
    (rd / "output.json").write_text(json.dumps(
        {"shots": [{"index": 1}, {"index": 2}]}), "utf-8")
    store.set_run_status(pid, S.STORYBOARD, rid, "done")
    r = eng.readiness(pid)
    assert r["storyboard"]["ready"] and r["storyboard"]["shots"] == 2
    assert r["first_frames"]["missing"] == [1, 2] and not r["video_gen"]["ready"]

    # first frames for shot 1 only -> still not ready
    fid, fd = store.create_run(pid, S.FIRST_FRAMES, {S.STORYBOARD: rid})
    (fd / "output.json").write_text(json.dumps(
        {"shots": [{"index": 1, "ok": True}]}), "utf-8")
    store.set_run_status(pid, S.FIRST_FRAMES, fid, "done")
    r = eng.readiness(pid)
    assert r["first_frames"]["missing"] == [2] and not r["video_gen"]["ready"]

    # both shots -> ready
    (fd / "output.json").write_text(json.dumps(
        {"shots": [{"index": 1, "ok": True}, {"index": 2, "ok": True}]}), "utf-8")
    r = eng.readiness(pid)
    assert r["first_frames"]["ready"] and r["video_gen"]["ready"]


async def test_chat_loop_with_mock_brain(env):
    store, eng = env
    pid = store.create("t", [{"type": "text", "ref": "橘猫短片"}])["id"]

    r1 = await eng.chat(pid, "我想做一个橘猫晒太阳的短片")
    assert r1["reply"] and eng.history(pid)[-1]["role"] == "assistant"
    # mock brain is JSON-schema flavored; chat tolerates any text
    r2 = await eng.chat(pid, "风格参考 $电影感")
    h = eng.history(pid)
    assert len(h) == 4 and h[0]["role"] == "user" and h[0]["content"] == "我想做一个橘猫晒太阳的短片"
    assert h[2]["content"] == "风格参考 $电影感"


async def test_chat_no_brain_configured(tmp_path):
    store = ProjectStore(tmp_path / "p")
    eng = ChatEngine(store, AppConfig())
    pid = store.create("t")["id"]
    r = await eng.chat(pid, "hello")
    assert "未配置" in r["reply"] and r["readiness"] is not None


async def test_chat_full_pipeline_ready_hint(tmp_path):
    store = ProjectStore(tmp_path / "p")
    engine = StageEngine(store, mock_config())
    pid = store.create("t", [{"type": "text", "ref": "橘猫"}])["id"]
    for stage in S.BRAIN_STAGES:
        r = await engine.run_stage(pid, stage)
        assert r["status"] == "done"
    eng = ChatEngine(store, mock_config())
    r = await eng.chat(pid, "素材都齐了吗？")
    assert r["readiness"]["storyboard"]["ready"]
    assert r["readiness"]["first_frames"]["missing"] == [1, 2]
    assert not r["readiness"]["video_gen"]["ready"]
