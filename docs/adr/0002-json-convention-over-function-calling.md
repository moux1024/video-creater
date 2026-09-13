# ADR 0002: Chat-completions-only brain with a JSON output convention

## Status

Accepted

## Context

The agent brain must accept any OpenAI-compatible chat-completions endpoint, including relays and local models (e.g. Ollama) that often lack function calling. Alternatives:

1. Require function calling/tools: reliable orchestration, but excludes many usable endpoints.
2. Support tools with a JSON fallback: best experience, but two orchestration paths to maintain and test.
3. Always use a prompt-defined structured-JSON convention, parsed and validated server-side.

## Decision

Option 3. On parse/validation failure, retry automatically with the previous error appended to the request (default max 3 attempts); if still failing, mark the run failed but expose the raw output so the user can **manually adopt** (edit and record it as a new run) and keep moving.

## Consequences

- Widest model compatibility; one code path.
- Orchestration reliability depends on server-side parsing and repair-prompt quality; the manual-adopt escape hatch guarantees the pipeline never dead-ends.
