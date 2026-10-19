# codex-jev

Jev decision engine as a self-contained **Codex plugin**. It exposes two MCP
tools to Codex through a local stdio server:

- `jev` — structured routing (Choice), yes/no (Noul), and scoring (Score),
  batched in a single round-trip.
- `jev_guard` — a six-signal action gate returning `allow` / `ask` / `deny`.

The engine (`plugins/jev/jev_engine/`) is a byte-for-byte copy of the tested
[Hermes `jev` plugin](https://github.com/Mrmimee/hermes-plugin-jev), so the
plugin needs no Hermes installation. Only the transport layer (the MCP server
and skill) is Codex-specific.

## Requirements

- A Python interpreter that can `import system_one_adapter`. By default the
  launcher looks for `%USERPROFILE%\jev-starter\.venv` then `py`/`python` on
  PATH. Set `JEV_PYTHON` to override.
- `AGNES_API_KEY` in the environment, or a desktop `apikey.txt` with an
  `AgnesAi` entry (the engine reads this fallback itself).

## Install into Codex

```bash
codex plugin marketplace add Mrmimee/codex-jev
codex plugin add jev@codex-jev
```

Restart Codex (or open a new thread) to load `jev` and `jev_guard`.

## Semantics

Jev only returns structured judgments; it never calls other tools or grants
authorization. On a missing key/engine, timeout, or online failure it returns
`error` with no verdict — an error is never an `allow`.
