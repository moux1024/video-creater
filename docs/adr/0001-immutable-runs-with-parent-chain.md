# ADR 0001: Append-only immutable Runs with a parent chain

## Status

Accepted

## Context

The product's core promise is "return to any earlier step and continue" with full process visibility. The obvious designs were:

1. Overwrite-in-place: re-running a stage replaces its artifacts. Simple, but destroys history and conflicts with observability.
2. Full version tree (git-like branches). Powerful, but heavy to build and explain for v1.
3. Append-only numbered runs per stage, with a user-switchable "current run" pointer per stage.

## Decision

Option 3, plus:

- Every run records a **parent chain**: the references to the upstream-stage runs it consumed when executed.
- Switching a stage's current pointer marks a downstream run **stale** exactly when its parent chain disagrees with the upstream current run.
- Stale is a **hint only**: stale runs remain viewable, usable, and exportable. Nothing is blocked.
- Editing an artifact never mutates a run; manual edits are recorded as new runs (so history stays immutable).

## Consequences

- Re-running is always safe; alternatives can be explored without losing anything.
- Precise staleness costs only storing a few IDs per run.
- Storage grows monotonically; acceptable for local single-user use, cleanup tooling can come later.
