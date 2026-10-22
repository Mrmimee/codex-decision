---
name: decision-engine
description: Use for a genuine routing choice between several plausible tools or execution paths, to independently review a preselected choice with explicit evidence, or when a risky action needs a structured second opinion. Do not use for ordinary conversation, obvious tool choices, or every tool call.
metadata:
  short-description: Structured routing and action-risk checks
---

# Decision Engine

Decision is a calibrated decision engine, not a chat model. It returns structured answers through the `decision`, `decision_review`, and `decision_guard` MCP tools. Use it selectively.

## When To Call

- Use `decision` with `choice` or `choices` when two or more tools or execution paths are genuinely plausible and the choice affects the result.
- Use `decision_review` when you already formed a `primaryChoice` but the choice is materially uncertain and evidence or constraints could change it. The reviewer sees the candidates, evidence, and constraints, but not your preselection, to reduce anchoring; it returns an advisory recommendation, confidence, and abstain/adoptable state.
- Use `decision` with `noul`, `nouls`, `score`, or `scores` for a calibrated yes/no probability or rating judgment that is useful before proceeding.
- Use `decision_guard` when an action has material risk, such as destructive changes, credential access, external transmission, or a hard-to-reverse operation.
- Do not call Decision for ordinary conversation, obvious tool selection, or before every tool call. Do not call it merely to restate a decision you already know.

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

### Evidence-backed review

Form `primaryChoice` yourself first, then send the bounded candidate set and
only the evidence needed for this decision. Keep the reviewer advisory.

```json
{
  "point": "candidate_selection",
  "question": "Which metric matches the requested recognition basis?",
  "primaryChoice": "metric_a",
  "options": [
    {"id": "metric_a", "description": "Estimated revenue", "evidenceRefs": ["E1"]},
    {"id": "metric_b", "description": "Financially recognized revenue", "evidenceRefs": ["E2"]}
  ],
  "evidence": [
    {"id": "E1", "fact": "Includes orders not yet recognized"},
    {"id": "E2", "fact": "User explicitly asked for financial recognition"}
  ],
  "constraints": ["Choose only from the candidate set"],
  "minConfidence": 0.75,
  "abstainBelow": 0.5
}
```

```json
{
  "state": "Delete the generated cache directory",
  "guard": {"threshold": 0.75}
}
```

## Read Results

- `decision` returns `answers` keyed by question name, `details` (including `probabilities` and `confidence`), plus aliases for single questions and `engine`, `model`, `usage`, and `latency_seconds`.
- `decision_review` returns `status` (`ok`, `not_adoptable`, `abstained`, or `shadow`), `recommendation`, `confidence`, `agreement`, `adoptable`, thresholds, and `engine`/`model`.
- `decision_guard` returns `verdict` (`allow`, `ask`, or `deny`), `confidence`, `verdict_source`, the six `signals`, and thresholds.
- In `decision_review`, `adoptable` is only a confidence threshold signal. You remain responsible for the final decision and for any user confirmation.
- Treat `abstained` and `not_adoptable` as reasons to keep your own choice or ask for more evidence; never turn a reviewer result into permission to bypass a safety rule.
- Treat `ask` as a reason to pause and ask the user. Treat `deny` as a reason not to perform the action. `allow` is a second opinion, not authorization.
- When the result has `error`, no verdict was produced. Do not treat an error as `allow`; say that the guard did not run and continue under the user's and system's actual authorization rules.

After using Decision, include one concise visible summary line in the response, for example:

```text
Decision: route=browser (0.8s)
Decision Guard: ask (0.62) (1.1s)
```

Do not paste raw JSON unless the user asks for it.

## Availability

The embedded engine defaults to `baseten/elephant-1:free` through OpenRouter
and needs `OPENROUTER_API_KEY` (or `DECISION_KEY_FILE` pointing at a single-line key
file) plus an importable `system_one_adapter`. It never scans your
Desktop or any other location for secrets. A missing key, missing engine,
timeout, or failed call returns an explicit `error`, not a fallback verdict.
