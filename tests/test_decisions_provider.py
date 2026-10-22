import json
import unittest
from pathlib import Path
from unittest.mock import patch

from system_one_adapter.providers.base import Message

import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "decision"))

from decision_engine.decisions_provider import (  # noqa: E402
    DecisionsProvider,
    _question_from_schema,
)


SCHEMA = {
    "$defs": {
        "TypeSafeAnswers": {
            "type": "object",
            "properties": {
                "team": {
                    "type": "object",
                    "description": "Which team should handle this?",
                    "properties": {
                        "billing": {"type": "number", "description": "Charges"},
                        "returns": {"type": "number", "description": "Returns"},
                    },
                },
                "safe": {"type": "number", "description": "Is it safe?"},
                "risk": {
                    "type": "integer",
                    "description": "How risky?",
                },
            },
        }
    },
    "properties": {"answers": {"$ref": "#/$defs/TypeSafeAnswers"}},
}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class DecisionsProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = DecisionsProvider(
            "baseten/elephant-1:free",
            base_url="https://openrouter.ai/api",
            api_key="test",
        )

    def test_recovers_question_text_from_probability_schema(self):
        node = {
            "type": "number",
            "description": (
                "Probability that the answer is yes or the assertion is true. "
                "0 means no or false, 0.5 means uncertain, and 1 means yes or true.\n"
                "Question: 该动作是否会读取凭据？"
            ),
        }
        question = _question_from_schema("reads_secrets", node)
        self.assertEqual(question["type"], "noul")
        self.assertEqual(question["instructions"], "该动作是否会读取凭据？")

    def test_distinct_questions_do_not_collapse(self):
        first = _question_from_schema("a", {
            "type": "number",
            "description": "Probability ...\nQuestion: 问题甲",
        })
        second = _question_from_schema("b", {
            "type": "number",
            "description": "Probability ...\nQuestion: 问题乙",
        })
        self.assertNotEqual(first["instructions"], second["instructions"])

    def test_uses_last_user_message_as_state(self):
        provider = DecisionsProvider(
            "baseten/elephant-1:free",
            base_url="https://openrouter.ai/api",
            api_key="test",
        )
        payload = {
            "answers": {
                "team": {
                    "type": "choice",
                    "choice": "returns",
                    "confidence": 0.8,
                    "probabilities": {"billing": 0.2, "returns": 0.8},
                },
                "safe": {"type": "noul", "noul": 0.9},
                "risk": {"type": "score", "score": 1.0},
            },
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        captured = {}

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data)
            return Response(payload)

        messages = [
            Message(role="system", content="system"),
            Message(role="user", content="<document>original</document>"),
            Message(role="assistant", content="bad output"),
            Message(role="user", content="correct it"),
        ]
        with patch("urllib.request.urlopen", fake_urlopen):
            result = provider.request(messages, schema=SCHEMA, structured=True)

        self.assertEqual(captured["body"]["state"], "<document>original</document>")
        self.assertEqual(
            captured["body"]["questions"]["team"]["criteria"],
            {"billing": "Charges", "returns": "Returns"},
        )
        self.assertEqual(captured["body"]["questions"]["safe"]["type"], "noul")
        self.assertEqual(captured["body"]["questions"]["risk"]["type"], "score")
        self.assertEqual(
            json.loads(result.text),
            {
                "answers": {
                    "team": {"billing": 0.2, "returns": 0.8},
                    "safe": 0.9,
                    "risk": 1,
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
