# JEV

JEV is a calibrated decision engine packaged as a self-contained **Codex plugin** (`decision` v1.0.1). It exposes three MCP
tools to Codex through a local stdio server:

- `decision` — structured routing (Choice), yes/no probability (Noul), and scoring (Score),
  batched in a single round-trip with full probabilities and confidence.
- `decision_review` — evidence-backed independent review of a preselected choice,
  with confidence bands, abstention, and optional shadow mode.
- `decision_guard` — a six-signal action gate returning `allow` / `ask` / `deny`.

The decision backend is `baseten/elephant-1:free` through OpenRouter's Decisions API.

### Self-contained layout

This plugin is fully self-contained (`plugins/decision/decision_engine/`):

- Cache and audit log live in `%LOCALAPPDATA%\codex-decision\decision\` (override with
  `DECISION_STATE_DIR`), outside the plugin cache so they survive updates.
- `system_one_adapter` is resolved from `DECISION_VENV` / `DECISION_SITE_PACKAGES`, or a
  local `~/jev-starter` or `~/.codex-decision` venv.

## Requirements

- Python with `system-one-adapter[openai]` installed. The launcher picks, in
  order: `DECISION_PYTHON`, `%USERPROFILE%\jev-starter\.venv`, `py -3`, `python`.
- `OPENROUTER_API_KEY` in the environment for the
  `baseten/elephant-1:free` backend. For headless setups that cannot export it,
  point `DECISION_KEY_FILE` at a single-line key file. The engine
  never scans your Desktop or any other location for secrets.

## Install into Codex

```bash
codex plugin marketplace add Mrmimee/codex-decision
codex plugin add decision@codex-decision
```

Restart Codex (or open a new thread) to load `decision`, `decision_review`, and
`decision_guard`.

## Semantics

JEV only returns structured judgments; it never calls other tools or grants
authorization. On a missing key/engine, timeout, or online failure it returns
`error` with no verdict — an error is never an `allow`.

`decision_review` deliberately asks the primary model to form `primaryChoice`
before calling it, but withholds that answer from the reviewer to reduce
anchoring. The reviewer sees bounded options, evidence, and constraints, and
returns an advisory recommendation with `confidence`, `agreement`, and
`adoptable`. A confidence below `abstainBelow` is reported as `abstained`; a
value below `minConfidence` is `not_adoptable`. Neither state grants permission
or replaces the primary model's final judgment.

## Guard policy

`decision_guard` runs six questions in one request and applies deterministic
rules in code. Missing or malformed signals are treated as a failure, not as a
zero, so an incomplete answer can never become an `allow`.

`allow` requires an operator policy that covers the action. Provide one with:

```bash
setx DECISION_OPERATOR_POLICY "Deleting cache and temp directories is routine. Git status and diff are routine."
```

or point `DECISION_POLICY_FILE` at a UTF-8 text file. The policy only relaxes
the final allow step; it can never bypass the credential, exfiltration, or
self-advocacy deny rules.
