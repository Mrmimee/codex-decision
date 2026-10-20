---
name: jev-decision-engine
description: Use for a genuine routing choice between several plausible tools or execution paths, or when a risky action needs a structured second opinion. Do not use for ordinary conversation, obvious tool choices, or every tool call.
metadata:
  short-description: Structured routing and action-risk checks
---

# Jev Decision Engine

Jev is a decision model, not a chat model. It returns structured answers through the `jev` and `jev_guard` MCP tools. Use it selectively.

## When To Call

- Use `jev` with `choice` or `choices` when two or more tools or execution paths are genuinely plausible and the choice affects the result.
- Use `jev` with `noul`, `nouls`, `score`, or `scores` for a discrete yes/no or rating judgment that is useful before proceeding.
- Use `jev_guard` when an action has material risk, such as destructive changes, credential access, external transmission, or a hard-to-reverse operation.
- Do not call Jev for ordinary conversation, obvious tool selection, or before every tool call. Do not call it merely to restate a decision you already know.

## Call Shape

Pass only the context needed for this judgment. Never include credentials or unrelated personal data.

```json
{
  "state": "The task and the plausible paths, without secrets",
  "choice": {
    "name": "route",
    "instructions": "Which path should handle this step?",
    "criteria": {
      "browser": "Navigate or inspect a web page",
      "computer_use": "Operate a desktop window or native UI"
    }
  },
  "timeout_seconds": 8
}
```

Batch independent questions with `choices`, `nouls`, and `scores` instead of making one call per question.

```json
{
  "state": "Delete the generated cache directory",
  "guard": {"threshold": 0.75}
}
```

## Read Results

- `jev` returns `answers` keyed by question name, plus aliases for single questions and `engine`, `model`, `usage`, and `latency_seconds`.
- `jev_guard` returns `verdict` (`allow`, `ask`, or `deny`), `verdict_source`, the six `signals`, and thresholds.
- Treat `ask` as a reason to pause and ask the user. Treat `deny` as a reason not to perform the action. `allow` is a second opinion, not authorization.
- When the result has `error`, no verdict was produced. Do not treat an error as `allow`; say that the guard did not run and continue under the user's and system's actual authorization rules.

After using Jev, include one concise visible summary line in the response, for example:

```text
Jev: route=browser (0.8s)
Jev Guard: ask (0.62) (1.1s)
```

Do not paste raw JSON unless the user asks for it.

## Availability

The embedded engine needs `AGNES_API_KEY` (or `JEV_KEY_FILE` pointing at a
single-line key file) and an importable `system_one_adapter`. It never scans
your Desktop or any other location for secrets. A missing key, missing engine,
timeout, or failed call returns an explicit `error`, not a fallback verdict.
