import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = ROOT / "plugins" / "decision" / "decision_engine" / "__init__.py"
spec = importlib.util.spec_from_file_location("decision_engine_under_test", ENGINE_PATH)
engine = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(engine)


def review_args(**overrides):
    args = {
        "point": "candidate_selection",
        "question": "Which metric matches the requested recognition basis?",
        "primaryChoice": "metric_a",
        "options": [
            {
                "id": "metric_a",
                "description": "Estimated revenue",
                "evidenceRefs": ["E1"],
            },
            {
                "id": "metric_b",
                "description": "Financially recognized revenue",
                "evidenceRefs": ["E2"],
            },
        ],
        "evidence": [
            {"id": "E1", "fact": "Includes orders not yet recognized"},
            {"id": "E2", "fact": "User explicitly asked for financial recognition"},
        ],
        "constraints": ["Choose only from the candidate set"],
    }
    args.update(overrides)
    return args


class ReviewTests(unittest.TestCase):
    def test_normalizes_bounded_review(self):
        review, min_confidence, abstain_below, shadow = engine._normalize_review_args(review_args())
        self.assertEqual(review["primaryChoice"], "metric_a")
        self.assertEqual(min_confidence, 0.75)
        self.assertEqual(abstain_below, 0.5)
        self.assertFalse(shadow)
        self.assertEqual([item["id"] for item in review["options"]], ["metric_a", "metric_b"])

    def test_rejects_primary_choice_outside_candidates(self):
        with self.assertRaisesRegex(ValueError, "primaryChoice"):
            engine._normalize_review_args(review_args(primaryChoice="metric_c"))

    def test_rejects_unknown_evidence_reference(self):
        options = review_args()["options"]
        options[0]["evidenceRefs"] = ["E9"]
        with self.assertRaisesRegex(ValueError, "不存在的证据"):
            engine._normalize_review_args(review_args(options=options))

    def test_rejects_invalid_threshold_order(self):
        with self.assertRaisesRegex(ValueError, "abstainBelow"):
            engine._normalize_review_args(review_args(minConfidence=0.6, abstainBelow=0.7))

    def test_rejects_secret_before_network(self):
        with patch.object(engine, "_execute_system_one_with_timeout") as call:
            result = json.loads(engine._handle_jev_review(
                review_args(question="Use Authorization: Bearer abcdefghijklmnop for this choice")
            ))
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["adoptable"])
        call.assert_not_called()

    def test_reviewer_does_not_see_primary_choice(self):
        review, _, _, _ = engine._normalize_review_args(review_args())
        state, question = engine._review_state_and_question(review)
        self.assertNotIn("primaryChoice", state)
        self.assertEqual(set(question.criteria), {"metric_a", "metric_b"})

    def test_projection_marks_adoptable(self):
        review, minimum, abstain, shadow = engine._normalize_review_args(review_args())
        result = engine._review_projection(
            type("Answer", (), {"choice": "metric_b", "confidence": 0.91})(),
            review,
            minimum,
            abstain,
            shadow,
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["adoptable"])
        self.assertFalse(result["agreement"])
        self.assertEqual(result["recommendation"], "metric_b")

    def test_projection_abstains_and_hides_shadow_suggestion(self):
        review, minimum, abstain, _ = engine._normalize_review_args(review_args())
        low = engine._review_projection(
            type("Answer", (), {"choice": "metric_b", "confidence": 0.3})(),
            review,
            minimum,
            abstain,
            False,
        )
        self.assertEqual(low["status"], "abstained")
        self.assertFalse(low["adoptable"])
        shadow = engine._review_projection(
            type("Answer", (), {"choice": "metric_b", "confidence": 0.9})(),
            review,
            minimum,
            abstain,
            True,
        )
        self.assertEqual(shadow["status"], "shadow")
        self.assertNotIn("recommendation", shadow)
        self.assertNotIn("confidence", shadow)
        self.assertNotIn("probabilities", shadow)

    def test_handler_returns_structured_result_without_live_network(self):
        response = type(
            "Response",
            (),
            {
                "choices": {
                    "review": type(
                        "Answer",
                        (),
                        {
                            "choice": "metric_b",
                            "confidence": 0.91,
                            "probabilities": {"metric_a": 0.09, "metric_b": 0.91},
                        },
                    )()
                },
                "engine": "test-engine",
                "model": "test-model",
                "usage": None,
            },
        )()
        with patch.object(engine, "_jev_tool_available", return_value=True), patch.object(
            engine, "_execute_system_one_with_timeout", return_value=response
        ), patch.object(engine, "_build_provider", return_value=object()):
            result = json.loads(engine._handle_jev_review(review_args(no_cache=True)))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["recommendation"], "metric_b")
        self.assertEqual(result["engine"], "test-engine")

    def test_unavailable_has_no_false_verdict(self):
        with patch.object(engine, "_jev_tool_available", return_value=False):
            result = json.loads(engine._handle_jev_review(review_args(no_cache=True)))
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["adoptable"])
        self.assertNotIn("recommendation", result)

    def test_invalid_input_does_not_call_network(self):
        with patch.object(engine, "_execute_system_one_with_timeout") as call:
            result = json.loads(engine._handle_jev_review(review_args(primaryChoice="metric_c")))
        self.assertEqual(result["status"], "rejected")
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
