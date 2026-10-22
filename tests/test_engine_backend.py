import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "decision"
sys.path.insert(0, str(PLUGIN))
engine = importlib.import_module("decision_engine")


class BackendSelectionTests(unittest.TestCase):
    def test_defaults_to_openrouter_elephant(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-key"}, clear=True):
            self.assertEqual(
                engine._active_engine(), "openrouter:baseten/elephant-1:free"
            )
            self.assertTrue(engine._decision_tool_available())

    def test_missing_selected_key_has_no_engine(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(engine._active_engine(), "none")
            self.assertFalse(engine._decision_tool_available())

    def test_build_provider_uses_decisions_adapter(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-key"}, clear=True):
            provider = engine._build_provider()
        self.assertEqual(type(provider).__name__, "DecisionsProvider")
        self.assertEqual(provider.model_name, "baseten/elephant-1:free")


if __name__ == "__main__":
    unittest.main()
