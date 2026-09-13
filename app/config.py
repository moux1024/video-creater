"""Role-based model configuration.

Each role (brain / text / image / video) points at an arbitrary OpenAI-compatible
endpoint with its own key. Stored in ~/.video-creater/config.toml (0600).
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
    import tomli_w  # optional, for saving
except ImportError:  # pragma: no cover
    tomllib = None

from pydantic import BaseModel

ROLE_BRAIN = "brain"
ROLE_TEXT = "text"
ROLE_IMAGE = "image"
ROLE_VIDEO = "video"
ROLES = (ROLE_BRAIN, ROLE_TEXT, ROLE_IMAGE, ROLE_VIDEO)


class ModelConfig(BaseModel):
    role: str
    provider: str = ""          # informational label, e.g. "kling", "openai-relay"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    extra: dict = {}            # provider-specific fields (e.g. kling secret)


class AppConfig(BaseModel):
    models: dict[str, ModelConfig] = {}
    # Global system prompt applied to all projects (layer 1 of user prompts).
    global_prompt: str = ""

    def model_for(self, role: str) -> ModelConfig | None:
        cfg = self.models.get(role)
        return cfg if cfg and cfg.base_url else None


DEFAULT_DIR = Path(os.environ.get("VIDEO_CREATER_HOME", Path.home() / ".video-creater"))


def load_config(path: Path | None = None) -> AppConfig:
    path = path or (DEFAULT_DIR / "config.toml")
    if not path.exists():
        return AppConfig()
    data = tomllib.loads(path.read_text("utf-8"))
    models = {}
    for role, raw in (data.get("models") or {}).items():
        if role not in ROLES:
            continue
        raw = dict(raw)
        raw.setdefault("extra", {})
        models[role] = ModelConfig(role=role, **raw)
    return AppConfig(models=models, global_prompt=data.get("prompt", {}).get("system", ""))


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    path = path or (DEFAULT_DIR / "config.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"models": {}, "prompt": {"system": cfg.global_prompt}}
    for role, m in cfg.models.items():
        data["models"][role] = {
            "provider": m.provider, "base_url": m.base_url,
            "api_key": m.api_key, "model": m.model, "extra": m.extra,
        }
    import tomli_w
    path.write_bytes(tomli_w.dumps(data).encode("utf-8"))
    path.chmod(0o600)
    return path
