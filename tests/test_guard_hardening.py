import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "decision"))
engine = importlib.import_module("decision_engine")


def signals(**overrides):
    base = {
        "verdict": "allow",
        "policy_allows": 0.9,
        "reads_secrets": 0.0,
        "sends_outbound": 0.0,
        "self_advocating": 0.0,
        "blast_radius": 0.0,
    }
    base.update(overrides)
    return base


class GuardHardeningTests(unittest.TestCase):
    def test_missing_signal_is_not_a_zero(self):
        self.assertIsNone(engine._guard_verdict(signals(reads_secrets=None), 0.75, True))
        self.assertIsNone(engine._guard_verdict(signals(blast_radius=None), 0.75, True))

    def test_invalid_model_verdict_is_rejected(self):
        self.assertIsNone(engine._guard_verdict(signals(verdict="maybe"), 0.75, True))

    def test_secrets_exfiltration_denies(self):
        verdict, _ = engine._guard_verdict(
            signals(reads_secrets=0.8, sends_outbound=0.8), 0.75, True
        )
        self.assertEqual(verdict, "deny")

    def test_secret_read_alone_denies(self):
        verdict, _ = engine._guard_verdict(signals(reads_secrets=0.8), 0.75, True)
        self.assertEqual(verdict, "deny")

    def test_self_advocating_forces_ask(self):
        verdict, _ = engine._guard_verdict(signals(self_advocating=0.7), 0.75, True)
        self.assertEqual(verdict, "ask")

    def test_non_trivial_blast_radius_forces_ask(self):
        verdict, _ = engine._guard_verdict(signals(blast_radius=1.0), 0.75, True)
        self.assertEqual(verdict, "ask")

    def test_model_deny_is_preserved(self):
        verdict, _ = engine._guard_verdict(signals(verdict="deny"), 0.75, True)
        self.assertEqual(verdict, "deny")

    def test_policy_allows_only_with_policy(self):
        with_policy, _ = engine._guard_verdict(signals(), 0.75, True)
        self.assertEqual(with_policy, "allow")
        without_policy, _ = engine._guard_verdict(signals(), 0.75, False)
        self.assertEqual(without_policy, "ask")

    def test_operator_policy_is_read_from_environment(self):
        with patch.dict(
            os.environ,
            {"DECISION_OPERATOR_POLICY": "deleting cache dirs is routine"},
            clear=True,
        ):
            self.assertEqual(
                engine._operator_policy(), "deleting cache dirs is routine"
            )

    def test_operator_policy_is_empty_without_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(engine._operator_policy(), "")


class GuardResponseTests(unittest.TestCase):
    def _response(self, **overrides):
        sig = signals(**overrides)
        return type(
            "Response",
            (),
            {
                "choices": {
                    "verdict": type(
                        "ChoiceAnswer",
                        (),
                        {"choice": sig["verdict"], "confidence": 0.9},
                    )()
                },
                "nouls": {
                    "guard_q": type("NoulAnswer", (), {"noul": 0.9})(),
                    "policy_allows": type("NoulAnswer", (), {"noul": sig["policy_allows"]})(),
                    "reads_secrets": type("NoulAnswer", (), {"noul": sig["reads_secrets"]})(),
                    "sends_outbound": type("NoulAnswer", (), {"noul": sig["sends_outbound"]})(),
                    "self_advocating": type("NoulAnswer", (), {"noul": sig["self_advocating"]})(),
                },
                "scores": {
                    "blast_radius": type("ScoreAnswer", (), {"score": sig["blast_radius"]})()
                },
                "engine": "openrouter",
                "model": "baseten/elephant-1:free",
            },
        )()

    def _run(self, response, **env):
        with patch.dict(os.environ, env, clear=True), patch.object(
            engine, "_decision_tool_available", return_value=True
        ), patch.object(
            engine, "_execute_system_one_with_timeout", return_value=response
        ), patch.object(engine, "_build_provider", return_value=object()):
            return json.loads(
                engine._handle_decision_guard(
                    {"state": "git status", "no_cache": True}
                )
            )

    def test_without_policy_allow_becomes_ask(self):
        result = self._run(self._response())
        self.assertEqual(result["verdict"], "ask")
        self.assertFalse(result["has_policy"])

    def test_with_policy_allow_is_allowed(self):
        result = self._run(
            self._response(), DECISION_OPERATOR_POLICY="git status is routine"
        )
        self.assertEqual(result["verdict"], "allow")
        self.assertTrue(result["has_policy"])

    def test_missing_signal_produces_error_not_a_verdict(self):
        response = self._response()
        response.nouls["reads_secrets"] = None
        with patch.dict(os.environ, {}, clear=True), patch.object(
            engine, "_decision_tool_available", return_value=True
        ), patch.object(
            engine, "_execute_system_one_with_timeout", return_value=response
        ), patch.object(engine, "_build_provider", return_value=object()):
            result = json.loads(
                engine._handle_decision_guard(
                    {"state": "git status", "no_cache": True}
                )
            )
        self.assertIn("error", result)
        self.assertNotIn("verdict", result)


if __name__ == "__main__":
    unittest.main()
