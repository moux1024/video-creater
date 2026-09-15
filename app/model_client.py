"""ModelClient — OpenAI-compatible chat completions + structured-JSON convention.

The brain requires only chat completions (streaming-capable). Tool calling is
emulated with a prompt-defined JSON convention, parsed & validated server-side.
Failure policy: retry with error appended (default 3 attempts), then surface
raw output for manual adoption (ADR 0002).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from .config import ModelConfig

DEFAULT_MAX_ATTEMPTS = 3


class BrainError(Exception):
    pass


class JSONParseError(BrainError):
    def __init__(self, raw: str, reason: str):
        super().__init__(reason)
        self.raw = raw
        self.reason = reason


def extract_json(text: str) -> dict:
    """Parse a JSON object from a completion; tolerate markdown fences / prose."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # last resort: outermost braces
        s, e = text.find("{"), text.rfind("}")
        if s < 0 or e <= s:
            raise JSONParseError(text, "no JSON object found")
        try:
            obj = json.loads(text[s:e + 1])
        except json.JSONDecodeError as ex:
            raise JSONParseError(text, f"invalid JSON: {ex}") from ex
    if not isinstance(obj, dict):
        raise JSONParseError(text, "top-level JSON is not an object")
    return obj


@dataclass
class BrainResult:
    ok: bool
    data: dict | None      # parsed JSON when ok
    raw: str               # last raw completion (for manual adoption on failure)
    attempts: int
    error: str = ""


class ModelClient:
    """Chat-completions client for one configured model role."""

    def __init__(self, cfg: ModelConfig, http: httpx.AsyncClient | None = None,
                 timeout: float = 120.0):
        self.cfg = cfg
        self.http = http or httpx.AsyncClient(timeout=timeout)

    async def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        from .mock_models import is_mock, mock_brain_response
        if is_mock(self.cfg.base_url):
            user = next((m["content"] for m in reversed(messages)
                         if m["role"] == "user"), "")
            return mock_brain_response(user)
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        body = {"model": self.cfg.model, "messages": messages, "temperature": temperature}
        resp = await self.http.post(url, json=body, headers=headers)
        if resp.status_code != 200:
            raise BrainError(f"{resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as ex:
            raise BrainError(f"unexpected response shape: {data}") from ex

    async def chat_json(self, system_prompt: str, user_prompt: str,
                        max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> BrainResult:
        """Chat with JSON-convention repair loop. Never raises; returns BrainResult."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        last_raw, last_err = "", ""
        for attempt in range(1, max_attempts + 1):
            try:
                raw = await self.chat(messages)
            except BrainError as ex:
                last_raw, last_err = "", str(ex)
                messages.append({"role": "user",
                                 "content": f"Your call failed with: {ex}. Try again."})
                continue
            last_raw = raw
            try:
                return BrainResult(ok=True, data=extract_json(raw), raw=raw, attempts=attempt)
            except JSONParseError as ex:
                last_err = ex.reason
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user",
                                 "content": f"That was not valid JSON ({ex.reason}). "
                                            f"Respond again with ONLY the JSON object."})
        return BrainResult(ok=False, data=None, raw=last_raw,
                           attempts=max_attempts, error=last_err)

    async def generate_image(self, prompt: str, out_path) -> bytes | None:
        """Image generation via OpenAI-compatible images/generations (best effort)."""
        from .mock_models import is_mock, png_bytes
        if is_mock(self.cfg.base_url):
            blob = png_bytes()
            with open(out_path, "wb") as f:
                f.write(blob)
            return blob
        url = self.cfg.base_url.rstrip("/") + "/images/generations"
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        body = {"model": self.cfg.model, "prompt": prompt, "n": 1,
                "response_format": "b64_json"}
        resp = await self.http.post(url, json=body, headers=headers)
        if resp.status_code != 200:
            raise BrainError(f"image gen {resp.status_code}: {resp.text[:300]}")
        import base64
        item = resp.json()["data"][0]
        if "b64_json" in item:
            blob = base64.b64decode(item["b64_json"])
        else:
            r2 = await self.http.get(item["url"])
            blob = r2.content
        with open(out_path, "wb") as f:
            f.write(blob)
        return blob
