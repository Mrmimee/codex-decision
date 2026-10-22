"""OpenRouter Decisions API provider for ``system_one_adapter``."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from typesafe_sdk import TypeSafeError
from system_one_adapter.providers.base import Message, ProviderResult


def _resolve_ref(schema: dict[str, Any], node: Any) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        cur: Any = schema
        for part in node["$ref"].lstrip("#/").split("/"):
            cur = cur[part]
        node = cur
    return node


def _question_text(name: str, node: dict[str, Any]) -> str:
    """Recover the original question text from the generated answer schema.

    In probability mode the description starts with a generic preamble and
    carries the real question on a ``Question:`` line, so the first line must
    not be used: that would send the same instruction for every question.
    """
    lines = node.get("description", "").splitlines()
    for line in lines:
        if line.startswith("Question:"):
            return line[len("Question:"):].strip()
    return lines[0] if lines else name


def _question_from_schema(name: str, node: dict[str, Any]) -> dict[str, Any]:
    lines = node.get("description", "").splitlines()
    instructions = _question_text(name, node)
    kind = node.get("type")

    if kind in ("number", "boolean"):
        return {"type": "noul", "instructions": instructions}
    if kind == "string" and node.get("enum"):
        return {"type": "choice", "instructions": instructions, "criteria": {k: k for k in node["enum"]}}
    if kind == "integer":
        criteria = [line.split(" = ", 1)[1] for line in lines if " = " in line]
        if not criteria:
            upper = node.get("maximum") or node.get("exclusiveMaximum")
            levels = int(upper) if isinstance(upper, (int, float)) else 3
            criteria = [f"level {i}" for i in range(max(2, levels))]
        return {"type": "score", "instructions": instructions, "criteria": criteria}
    if kind == "object" and node.get("properties"):
        criteria = {k: (v.get("description") or k) for k, v in node["properties"].items()}
        return {"type": "choice", "instructions": instructions, "criteria": criteria}
    raise TypeSafeError(f"Unsupported Decisions answer schema for {name}: {node}")


def _extract_answer(node: dict[str, Any], answer: dict[str, Any]) -> Any:
    kind = node.get("type")
    if kind == "number":
        return float(answer.get("noul", 0.0))
    if kind == "boolean":
        return bool(float(answer.get("noul", 0.0)) >= 0.5)
    if kind == "string" and node.get("enum"):
        return answer.get("choice")
    if kind == "integer":
        return int(round(float(answer.get("score", 0.0))))
    probs = answer.get("probabilities", {})
    return {label: float(probs.get(label, 0.0)) for label in node.get("properties", {})}


class DecisionsProvider:
    """Minimal sync provider for OpenRouter's Decisions endpoint."""

    _RETRY_STATUS = (429, 500, 502, 503, 529)
    _RETRY_ATTEMPTS = 3
    _RETRY_BASE_DELAY = 0.6

    def __init__(self, model_name: str, *, base_url: str, api_key: str) -> None:
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def translate_error(self, error: Exception) -> TypeSafeError:
        return error if isinstance(error, TypeSafeError) else TypeSafeError(str(error))

    def request(self, messages: list[Message], *, schema: dict[str, Any], structured: bool) -> ProviderResult:
        del structured
        properties = _resolve_ref(schema, {"$ref": "#/properties/answers"}).get("properties", {})
        resolved = {name: _resolve_ref(schema, node) for name, node in properties.items()}
        if not resolved:
            raise TypeSafeError("The generated schema contained no questions.")
        document = next(
            (m.content for m in messages if m.role == "user" and m.content.lstrip().startswith("<document>")),
            None,
        )
        if document is None:
            raise TypeSafeError("No <document> message was supplied to DecisionsProvider.")
        body = {
            "model": self.model_name,
            "state": document,
            "questions": {name: _question_from_schema(name, node) for name, node in resolved.items()},
        }
        payload = self._post(body)

        raw_answers = payload.get("answers", {})
        answers = {name: _extract_answer(node, raw_answers.get(name, {})) for name, node in resolved.items()}
        usage = payload.get("usage", {})
        return ProviderResult(
            text=json.dumps({"answers": answers}),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST with retries for transient failures only.

        Retryable: 429/5xx responses, dropped connections, and TLS EOF.
        A 4xx (bad request, unauthorized, over quota) fails immediately because
        retrying cannot change the outcome.
        """
        request = urllib.request.Request(
            self.base_url + "/alpha/decisions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self._RETRY_ATTEMPTS):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")[:500]
                if error.code not in self._RETRY_STATUS:
                    raise TypeSafeError(f"HTTP {error.code}: {detail}") from error
                last_error = error
            except (urllib.error.URLError, OSError) as error:
                last_error = error
            if attempt < self._RETRY_ATTEMPTS - 1:
                time.sleep(self._RETRY_BASE_DELAY * (2**attempt))
        raise TypeSafeError(f"Decisions request failed: {last_error}")
