import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "jev"
sys.path.insert(0, str(PLUGIN))
ENGINE_PATH = PLUGIN / "jev_engine" / "__init__.py"
engine = importlib.import_module("jev_engine")


class BackendSelectionTests(unittest.TestCase):
    def test_defaults_to_openrouter_elephant(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-key"}, clear=True):
            self.assertEqual(engine._get_engine_name(), "openrouter")
            self.assertEqual(
                engine._active_engine(), "openrouter:baseten/elephant-1:free"
            )
            self.assertTrue(engine._jev_tool_available())

    def test_explicit_agnes_fallback(self):
        with patch.dict(
            os.environ,
            {"JEV_ENGINE": "agnes", "AGNES_API_KEY": "agnes-key"},
            clear=True,
        ):
            self.assertEqual(engine._get_engine_name(), "agnes")
            self.assertEqual(engine._active_engine(), "agnes:agnes-3.0-flash")
            self.assertTrue(engine._jev_tool_available())

    def test_missing_selected_key_has_no_engine(self):
        with patch.dict(os.environ, {"JEV_ENGINE": "agnes"}, clear=True):
            self.assertEqual(engine._active_engine(), "none")
            self.assertFalse(engine._jev_tool_available())

    def test_guard_defaults_to_agnes(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-key"}, clear=True):
            self.assertEqual(engine._guard_engine_name(), "agnes")
            self.assertEqual(engine._active_guard_engine(), "none")
        with patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "or-key", "AGNES_API_KEY": "agnes-key"},
            clear=True,
        ):
            self.assertEqual(engine._active_guard_engine(), "agnes:agnes-3.0-flash")

    def test_guard_can_explicitly_use_openrouter(self):
        with patch.dict(
            os.environ,
            {"JEV_GUARD_ENGINE": "openrouter", "OPENROUTER_API_KEY": "or-key"},
            clear=True,
        ):
            self.assertEqual(engine._guard_engine_name(), "openrouter")
            self.assertEqual(
                engine._active_guard_engine(),
                "openrouter:baseten/elephant-1:free",
            )

    def test_build_provider_uses_decisions_adapter(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-key"}, clear=True):
            provider = engine._build_provider()
        self.assertEqual(type(provider).__name__, "DecisionsProvider")
        self.assertEqual(provider.model_name, "baseten/elephant-1:free")

    def test_build_provider_uses_agnes_when_requested(self):
        with patch.dict(
            os.environ,
            {"JEV_ENGINE": "agnes", "AGNES_API_KEY": "agnes-key"},
            clear=True,
        ), patch.object(engine, "OpenAIProvider") as provider:
            engine._build_provider()
        provider.assert_called_once()
        self.assertEqual(provider.call_args.args[0], "agnes-3.0-flash")


if __name__ == "__main__":
    unittest.main()
