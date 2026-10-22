# codex-jev

Jev decision engine as a self-contained **Codex plugin**. It exposes three MCP
tools to Codex through a local stdio server:

- `jev` — structured routing (Choice), yes/no (Noul), and scoring (Score),
  batched in a single round-trip.
- `jev_review` — evidence-backed independent review of a preselected choice,
  with confidence bands, abstention, and optional shadow mode.
- `jev_guard` — a six-signal action gate returning `allow` / `ask` / `deny`.

The default decision backend is `baseten/elephant-1:free` through OpenRouter.
The guard keeps Agnes as its default safety backend because it produced more
conservative risk signals in testing. Set `JEV_ENGINE=agnes` to move ordinary
decisions back to Agnes, or `JEV_GUARD_ENGINE=openrouter` to move the guard to
Elephant.

The engine (`plugins/jev/jev_engine/`) is the tested upstream
[Hermes `jev` plugin](https://github.com/Mrmimee/hermes-plugin-jev) logic with
its Hermes registration shim removed. Only the transport layer (the MCP server
and skill) is Codex-specific.

### No Hermes required

This plugin is fully self-contained. It does not read, write, or import
anything from Hermes:

- Cache and audit log live in `%LOCALAPPDATA%\codex-jev\jev\` (override with
  `JEV_STATE_DIR`), outside the plugin cache so they survive updates.
- `system_one_adapter` is resolved from `JEV_VENV` / `JEV_SITE_PACKAGES`, or a
  local `~/jev-starter` or `~/.jev` venv — none of which are Hermes paths.

## Requirements

- Python with `system-one-adapter[openai]` installed. The launcher picks, in
  order: `JEV_PYTHON`, `%USERPROFILE%\jev-starter\.venv`, `py -3`, `python`.
- `OPENROUTER_API_KEY` in the environment for the default
  `baseten/elephant-1:free` backend. For headless setups that cannot export it,
  point `JEV_KEY_FILE` at a single-line key file. Set `JEV_ENGINE=agnes` and
  provide `AGNES_API_KEY` to use the legacy Agnes backend instead. The engine
  never scans your Desktop or any other location for secrets.

## Install into Codex

```bash
codex plugin marketplace add Mrmimee/codex-jev
codex plugin add jev@codex-jev
```

Restart Codex (or open a new thread) to load `jev`, `jev_review`, and
`jev_guard`.

## Semantics

Jev only returns structured judgments; it never calls other tools or grants
authorization. On a missing key/engine, timeout, or online failure it returns
`error` with no verdict — an error is never an `allow`.

`jev_review` deliberately asks the primary model to form `primaryChoice`
before calling it, but withholds that answer from the reviewer to reduce
anchoring. The reviewer sees bounded options, evidence, and constraints, and
returns an advisory recommendation with `confidence`, `agreement`, and
`adoptable`. A confidence below `abstainBelow` is reported as `abstained`; a
value below `minConfidence` is `not_adoptable`. Neither state grants permission
or replaces the primary model's final judgment.
