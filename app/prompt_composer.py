"""PromptComposer — layered system-prompt assembly.

Order (append-only, later layers take precedence on conflict):
  1. built-in base prompt (agent role + JSON protocol; not user-overridable)
  2. global system prompt
  3. project system prompt
  4. activated skill contents (markdown files)
  5. optional stage-level override (brain stages only)
"""
from __future__ import annotations

from dataclasses import dataclass, field

BASE_PROMPT = """\
You are the agent brain of a video-creation pipeline. You follow the pipeline's
stage instructions and ALWAYS respond with a single JSON object (no prose, no
markdown fences) conforming to the schema given in the stage instruction.
If part of the request is ambiguous, make a reasonable creative choice and
record it under "notes" in your JSON output.
"""


@dataclass
class PromptLayers:
    global_prompt: str = ""
    project_prompt: str = ""
    skills: list[str] = field(default_factory=list)   # each skill's full markdown content
    stage_override: str = ""


@dataclass
class ComposedPrompt:
    text: str
    provenance: list[str] = field(default_factory=list)  # which layers contributed


class PromptComposer:
    """Deep module: layers in -> assembled prompt + provenance."""

    def compose(self, layers: PromptLayers) -> ComposedPrompt:
        parts: list[tuple[str, str]] = [("base", BASE_PROMPT)]
        if layers.global_prompt.strip():
            parts.append(("global", layers.global_prompt.strip()))
        if layers.project_prompt.strip():
            parts.append(("project", layers.project_prompt.strip()))
        for i, skill in enumerate(layers.skills):
            if skill.strip():
                parts.append((f"skill[{i}]", skill.strip()))
        if layers.stage_override.strip():
            parts.append(("stage_override", layers.stage_override.strip()))
        text = "\n\n".join(f"# --- {name} ---\n{body}" for name, body in parts)
        return ComposedPrompt(text=text, provenance=[name for name, _ in parts])

    def estimate_tokens(self, layers: PromptLayers) -> int:
        # rough heuristic: ~1 token per 3.5 chars for mixed CJK/EN
        return len(self.compose(layers).text) // 3 + 1
