import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "jev"))
engine = importlib.import_module("jev_engine")


class GuardPolicyTests(unittest.TestCase):
    def test_self_advocating_forces_ask(self):
        verdict, _ = engine._guard_verdict_from_signals(
            {
                "verdict": "allow",
                "policy_allows": 0.99,
                "reads_secrets": 0.0,
                "sends_outbound": 0.0,
                "self_advocating": 0.7,
                "blast_radius": 0.0,
            },
            0.75,
        )
        self.assertEqual(verdict, "ask")

    def test_secrets_exfiltration_denies(self):
        verdict, _ = engine._guard_verdict_from_signals(
            {
                "verdict": "allow",
                "policy_allows": 0.99,
                "reads_secrets": 0.8,
                "sends_outbound": 0.8,
                "self_advocating": 0.0,
                "blast_radius": 0.0,
            },
            0.75,
        )
        self.assertEqual(verdict, "deny")

    def test_model_deny_is_preserved(self):
        verdict, _ = engine._guard_verdict_from_signals(
            {
                "verdict": "deny",
                "policy_allows": 0.9,
                "reads_secrets": 0.0,
                "sends_outbound": 0.0,
                "self_advocating": 0.0,
                "blast_radius": 0.1,
            },
            0.75,
        )
        self.assertEqual(verdict, "deny")

    def test_low_confidence_allow_downgrades_to_ask(self):
        response = type(
            "Response",
            (),
            {
                "choices": {
                    "verdict": type(
                        "ChoiceAnswer",
                        (),
                        {"choice": "allow", "confidence": 0.3},
                    )()
                },
                "nouls": {
                    "policy_allows": type("NoulAnswer", (), {"noul": 0.2})(),
                    "reads_secrets": type("NoulAnswer", (), {"noul": 0.0})(),
                    "sends_outbound": type("NoulAnswer", (), {"noul": 0.0})(),
                    "self_advocating": type("NoulAnswer", (), {"noul": 0.0})(),
                },
                "scores": {
                    "blast_radius": type("ScoreAnswer", (), {"score": 0.0})()
                },
                "engine": "agnes",
                "model": "agnes-3.0-flash",
            },
        )()
        with patch.object(engine, "_guard_tool_available", return_value=True), patch.object(
            engine, "_execute_system_one_with_timeout", return_value=response
        ), patch.object(engine, "_build_provider", return_value=object()):
            result = json.loads(
                engine._handle_jev_guard({"state": "git status", "no_cache": True})
            )
        self.assertEqual(result["verdict"], "ask")
        self.assertEqual(result["confidence"], 0.3)


if __name__ == "__main__":
    unittest.main()
