"""HTTP-layer end-to-end smoke over the FastAPI app with mocked models."""
import json

import pytest

from app.server import build_app


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_e2e import mock_config
    app = build_app(config=mock_config(), projects_dir=tmp_path / "projects")
    with TestClient(app) as c:
        yield c


def test_http_full_pipeline(client):
    # settings visible
    cfg = client.get("/api/config").json()
    assert cfg["models"]["brain"]["base_url"] == "mock://local"

    # project + skill + prompt
    pid = client.post("/api/projects", json={
        "name": "http-e2e", "inputs": [{"type": "text", "ref": "日落延时"}]}).json()["id"]
    client.post(f"/api/projects/{pid}/skills",
                json={"filename": "电影感.md", "content": "冷色调"})
    client.patch(f"/api/projects/{pid}", json={
        "active_skills": ["电影感.md"], "project_prompt": "竖屏短视频"})

    # run all stages through the API
    order = ["intent", "storyboard", "shot_prompts", "first_frames", "video_gen", "export"]
    for stage in order:
        if stage == "export":
            # poll video tasks to done first
            for _ in range(2):
                r = client.post(f"/api/projects/{pid}/poll_video/run-0001").json()
        r = client.post(f"/api/projects/{pid}/stages/{stage}/runs", json={"note": ""})
        assert r.status_code == 200, r.text
        import time
        for _ in range(50):  # wait for background task
            d = client.get(f"/api/projects/{pid}/stages/{stage}/runs/run-0001").json()
            if d["run"]["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert d["run"]["status"] == "done", (stage, d["events"])
        assert d["output"] is not None

    # export produced a deliverable
    exp = client.get(f"/api/projects/{pid}/stages/export/runs/run-0001").json()
    assert exp["output"]["mode"] in ("ffmpeg", "materials")

    # video artifact downloadable
    vids = client.get(f"/api/projects/{pid}/stages/video_gen/runs/run-0001").json()
    vf = next(f for f in vids["files"] if f.endswith(".mp4"))
    r = client.get(f"/api/projects/{pid}/stages/video_gen/runs/run-0001/files/{vf}")
    assert r.status_code == 200 and len(r.content) > 10

    # input upload endpoint
    r = client.post(f"/api/projects/{pid}/inputs",
                    files={"file": ("ref.png", b"\x89PNG\r\n\x1a\nxx", "image/png")})
    assert r.status_code == 200
    r = client.get(f"/api/projects/{pid}/inputs/ref.png")
    assert r.status_code == 200
