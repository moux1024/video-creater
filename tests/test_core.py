import json

import pytest

from app.config import AppConfig, ModelConfig
from app.prompt_composer import PromptComposer, PromptLayers


class TestPromptComposer:
    def test_layers_appended_in_order_with_provenance(self):
        c = PromptComposer().compose(PromptLayers(
            global_prompt="全局", project_prompt="项目",
            skills=["技能A", ""], stage_override="阶段"))
        assert ["base", "global", "project", "skill[0]", "stage_override"] == c.provenance
        assert c.text.index("全局") < c.text.index("项目") < c.text.index("技能A") < c.text.index("阶段")
        assert "agent brain" in c.text  # base prompt always present, not overridable

    def test_empty_layers_yield_base_only(self):
        c = PromptComposer().compose(PromptLayers())
        assert c.provenance == ["base"]

    def test_token_estimate_grows_with_skills(self):
        p = PromptComposer()
        small = p.estimate_tokens(PromptLayers(global_prompt="hi"))
        big = p.estimate_tokens(PromptLayers(global_prompt="hi", skills=["x" * 5000]))
        assert big > small


class TestProjectStore:
    @pytest.fixture()
    def store(self, tmp_path):
        from app.project_store import ProjectStore
        return ProjectStore(tmp_path / "projects")

    def test_create_and_get_project(self, store):
        p = store.create("demo", [{"type": "text", "ref": "一只猫"}])
        assert store.get(p["id"])["name"] == "demo"
        assert set(p["stages"]) == {"intent", "storyboard", "shot_prompts",
                                    "first_frames", "video_gen", "export"}

    def test_runs_append_only_and_parent_chain(self, store):
        pid = store.create("d")["id"]
        r1, _ = store.create_run(pid, "intent", {})
        r2, _ = store.create_run(pid, "storyboard", {"intent": r1})
        assert r1 == "run-0001" and r2 == "run-0001"
        assert store.get_run(pid, "storyboard", r2)["parents"] == {"intent": r1}

    def test_current_run_switch_marks_stale(self, store):
        pid = store.create("d")["id"]
        i1, _ = store.create_run(pid, "intent", {})
        b1, _ = store.create_run(pid, "storyboard", {"intent": i1})
        view = store.stage_view(pid)
        assert view["storyboard"]["runs"][0]["stale"] is False
        i2, _ = store.create_run(pid, "intent", {})      # new intent current (i2)
        view = store.stage_view(pid)
        assert view["storyboard"]["runs"][0]["stale"] is True   # parent i1 != current i2
        store.set_current_run(pid, "intent", i1)          # switch back
        assert store.stage_view(pid)["storyboard"]["runs"][0]["stale"] is False

    def test_stage_override_rejected_on_non_brain_stage(self, store):
        pid = store.create("d")["id"]
        with pytest.raises(Exception):
            store.update(pid, {"stage_overrides": {"export": "x"}})
        store.update(pid, {"stage_overrides": {"intent": "x"}})  # ok

    def test_skills_activation(self, store):
        pid = store.create("d")["id"]
        store.write_skill(pid, "风格.md", "电影感")
        store.update(pid, {"active_skills": ["风格.md"]})
        skills = {s["filename"]: s for s in store.list_skills(pid)}
        assert skills["风格.md"]["active"] is True


class TestModelClientJSON:
    @pytest.fixture()
    def http(self):
        import httpx
        return httpx.AsyncClient(transport=httpx.MockTransport())

    def test_extract_json_plain(self):
        from app.model_client import extract_json
        assert extract_json('{"a": 1}') == {"a": 1}
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('好的，结果如下 {"a": 1} 请查收') == {"a": 1}
        with pytest.raises(Exception):
            extract_json("完全不是 JSON")

    async def test_chat_json_repair_loop(self):
        import httpx
        from app.model_client import ModelClient
        from app.config import ModelConfig
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            body = json.loads(request.content)
            user = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
            if "not valid JSON" in user:
                return httpx.Response(200, json={"choices": [
                    {"message": {"content": '{"fixed": true}'}}]})
            return httpx.Response(200, json={"choices": [
                {"message": {"content": '我认为应该是 {"a": 1'}}]})

        client = ModelClient(ModelConfig(role="brain", base_url="http://x", api_key="k",
                                         model="m"),
                             http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        res = await client.chat_json("sys", "do it")
        assert res.ok and res.data == {"fixed": True} and res.attempts == 2

    async def test_chat_json_gives_up_with_raw_for_manual_adoption(self):
        import httpx
        from app.model_client import ModelClient
        from app.config import ModelConfig

        def handler(request):
            return httpx.Response(200, json={"choices": [
                {"message": {"content": "no json here"}}]})

        client = ModelClient(ModelConfig(role="brain", base_url="http://x", api_key="k",
                                         model="m"),
                             http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        res = await client.chat_json("s", "u", max_attempts=2)
        assert not res.ok and res.raw == "no json here" and res.attempts == 2


class TestVideoProvider:
    async def test_kling_submit_poll_download_lifecycle(self, tmp_path):
        import httpx
        from app.config import ModelConfig
        from app.video_provider import get_provider, SubmitOptions

        state = {"polled": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("text2video"):
                return httpx.Response(200, json={"data": {"task_id": "t1"}})
            if "/videos/t1" in str(request.url):
                state["polled"] += 1
                if state["polled"] == 1:
                    return httpx.Response(200, json={"data": {"task_status": "processing"}})
                return httpx.Response(200, json={"data": {
                    "task_status": "succeed",
                    "task_result": {"videos": [{"url": "http://x/v.mp4"}]}}})
            return httpx.Response(200, content=b"FAKE_MP4",
                                  headers={"content-type": "video/mp4"})

        cfg = ModelConfig(role="video", provider="kling", base_url="http://x",
                          api_key="ak", extra={"secret_key": "sk"})
        p = get_provider(cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        task = await p.submit(SubmitOptions(prompt="a cat"))
        assert task.status == "submitted"
        task = await p.poll(task)
        assert task.status == "rendering"
        task = await p.poll(task)
        assert task.status == "done" and task.video_url
        out = tmp_path / "v.mp4"
        await p.download(task, out)
        assert out.read_bytes() == b"FAKE_MP4"

    def test_unknown_provider_rejected(self):
        from app.video_provider import get_provider
        from app.config import ModelConfig
        with pytest.raises(KeyError):
            get_provider(ModelConfig(role="video", provider="nope"))


class TestStageEngine:
    @pytest.fixture()
    def env(self, tmp_path):
        from app.project_store import ProjectStore
        from app.stage_engine import StageEngine

        def handler(request):
            return httpx.Response(200, json={"choices": [
                {"message": {"content": json.dumps({
                    "summary": "s", "shots": [
                        {"index": 1, "description": "d", "narration": "n",
                         "duration_seconds": 5}]})}}]})

        import httpx
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cfg = AppConfig(models={
            "brain": ModelConfig(role="brain", base_url="http://x", api_key="k", model="m"),
        }, global_prompt="GP")
        store = ProjectStore(tmp_path / "projects")
        engine = StageEngine(store, cfg, http_factory=lambda: client)
        return store, engine, client

    async def test_brain_pipeline_runs_and_logs(self, env):
        store, engine, _ = env
        pid = store.create("t", [{"type": "text", "ref": "hi"}])["id"]

        r = await engine.run_stage(pid, "intent")
        assert r["status"] == "done"
        events = [json.loads(line)["type"] for line in store.run_dir(
            pid, "intent", r["run_id"]).joinpath(
            "events.ndjson").read_text("utf-8").splitlines()]
        assert "system_prompt_assembled" in events

        r2 = await engine.run_stage(pid, "storyboard")
        assert r2["status"] == "done"

    async def test_storyboard_requires_intent(self, env):
        store, engine, _ = env
        pid = store.create("t")["id"]
        r = await engine.run_stage(pid, "storyboard")
        assert r["status"] == "failed" and "intent" in r["error"]

    async def test_failed_brain_saves_raw_for_manual_adoption(self, tmp_path):
        import httpx
        from app.project_store import ProjectStore
        from app.stage_engine import StageEngine

        def handler(request):
            return httpx.Response(200, json={"choices": [
                {"message": {"content": "garbage, no json"}}]})

        cfg = AppConfig(models={"brain": ModelConfig(role="brain", base_url="http://x",
                                                     api_key="k", model="m")})
        store = ProjectStore(tmp_path / "p")
        engine = StageEngine(store, cfg,
                             http_factory=lambda: httpx.AsyncClient(
                                 transport=httpx.MockTransport(handler)))
        pid = store.create("t")["id"]
        r = await engine.run_stage(pid, "intent")
        assert r["status"] == "failed"
        assert (store.run_dir(pid, "intent", r["run_id"]) / "raw_output.txt").exists()


class TestExporter:
    def test_degrades_to_materials_package_without_ffmpeg(self, tmp_path, monkeypatch):
        from app.exporter import Exporter
        monkeypatch.setattr("shutil.which", lambda _: None)

        class L:
            def emit(self, *a, **k): pass

        f1, f2 = tmp_path / "a.mp4", tmp_path / "b.mp4"
        f1.write_bytes(b"a"); f2.write_bytes(b"b")
        res = Exporter().export([str(f1), str(f2)], tmp_path / "run", L())
        assert res["mode"] == "materials" and len(res["files"]) == 2
        assert (tmp_path / "run" / "materials" / "a.mp4").exists()

    def test_ffmpeg_stitch(self, tmp_path):
        from app.exporter import Exporter
        import shutil
        if not shutil.which("ffmpeg"):
            pytest.skip("ffmpeg not installed")
        # build two 1s test clips
        run = tmp_path / "run"
        run.mkdir()
        clips = []
        for i in range(2):
            c = tmp_path / f"c{i}.mp4"
            import subprocess
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                            f"testsrc=duration=1:size=128x96:rate=10",
                            str(c)], capture_output=True, check=True)
            clips.append(str(c))

        class L:
            def emit(self, *a, **k): pass

        res = Exporter().export(clips, run, L())
        assert res["mode"] == "ffmpeg" and (run / "final.mp4").exists()
