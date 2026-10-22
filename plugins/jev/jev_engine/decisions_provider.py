"""OpenRouter Decisions provider for decision-only models such as Elephant 1.

Decision models reject the OpenAI-compatible Chat Completions endpoint. This
provider adapts the TypeSafe-shaped request produced by ``system_one_adapter``
to OpenRouter's Decisions API and wraps its structured answer back into the
single JSON document the adapter expects to decode.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from typesafe_sdk import TypeSafeError
from system_one_adapter.providers.base import Message, ProviderResult


def _resolve_ref(schema: dict[str, Any], node: Any) -> Any:
    """Resolve the local ``$ref`` nodes used by the generated output schema."""
    while isinstance(node, dict) and "$ref" in node:
        current: Any = schema
        for part in node["$ref"].lstrip("#/").split("/"):
            current = current[part]
        node = current
    return node


def _question_from_schema(name: str, node: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct one Decisions question from its generated answer schema."""
    description = node.get("description", "")
    lines = description.splitlines()
    instructions = lines[0] if lines else name
    kind = node.get("type")

    if kind in ("number", "boolean"):
        return {"type": "noul", "instructions": instructions}
    if kind == "string" and node.get("enum"):
        return {
            "type": "choice",
            "instructions": instructions,
            "criteria": {label: label for label in node["enum"]},
        }
    if kind == "integer":
        criteria = [
            line.split(" = ", 1)[1] for line in lines if " = " in line
        ]
        if not criteria:
            # ``system_one_adapter``'s discrete Score schema does not put
            # the rubric in the field description. Use the schema's upper
            # bound when present; otherwise keep the conventional 3 levels
            # used by jev_guard (trivial / annoying / severe).
            upper = node.get("maximum") or node.get("exclusiveMaximum")
            levels = int(upper) if isinstance(upper, (int, float)) else 3
            criteria = [f"level {index}" for index in range(max(2, levels))]
        return {
            "type": "score",
            "instructions": instructions,
            "criteria": criteria,
        }
    if kind == "object" and node.get("properties"):
        criteria = {
            label: (prop.get("description") or label)
            for label, prop in node["properties"].items()
        }
        return {
            "type": "choice",
            "instructions": instructions,
            "criteria": criteria,
        }
    raise TypeSafeError(f"Unsupported Decisions answer schema for {name}: {node}")


class DecisionsProvider:
    """Minimal sync provider for OpenRouter's Decisions endpoint."""

    def __init__(self, model_name: str, *, base_url: str, api_key: str) -> None:
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def translate_error(self, error: Exception) -> TypeSafeError:
        if isinstance(error, TypeSafeError):
            return error
        return TypeSafeError(str(error))

    def request(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any],
        structured: bool,
    ) -> ProviderResult:
        # ``structured`` describes native JSON-schema support. Decisions
        # models are always structured, so the generated schema is still used
        # to reconstruct the typed questions.
        del structured
        answers_schema = _resolve_ref(schema, {"$ref": "#/properties/answers"})
        properties = answers_schema.get("properties", {})
        questions = {
            name: _question_from_schema(name, _resolve_ref(schema, node))
            for name, node in properties.items()
        }
        if not questions:
            raise TypeSafeError("The generated schema contained no questions.")

        document = next(
            (
                message.content
                for message in messages
                if message.role == "user"
                and message.content.lstrip().startswith("<document>")
            ),
            None,
        )
        if document is None:
            raise TypeSafeError("No <document> message was supplied to DecisionsProvider.")
        body = {
            "model": self.model_name,
            "state": document,
            "questions": questions,
        }
        request = urllib.request.Request(
            self.base_url + "/alpha/decisions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            raise TypeSafeError(f"HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise TypeSafeError(f"Decisions request failed: {error.reason}") from error

        answers: dict[str, Any] = {}
        for name, node in properties.items():
            node = _resolve_ref(schema, node)
            answer = payload.get("answers", {}).get(name, {})
            kind = node.get("type")
            if kind == "number":
                answers[name] = float(answer.get("noul", 0.0))
            elif kind == "boolean":
                answers[name] = bool(float(answer.get("noul", 0.0)) >= 0.5)
            elif kind == "string" and node.get("enum"):
                answers[name] = answer.get("choice")
            elif kind == "integer":
                answers[name] = int(round(float(answer.get("score", 0.0))))
            elif kind == "object":
                probabilities = answer.get("probabilities", {})
                answers[name] = {
                    label: float(probabilities.get(label, 0.0))
                    for label in node.get("properties", {})
                }

        usage = payload.get("usage", {})
        return ProviderResult(
            text=json.dumps({"answers": answers}),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
