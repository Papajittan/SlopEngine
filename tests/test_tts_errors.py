import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_env_loading import import_shorts_core_with_fakes


class TTSErrorTests(unittest.TestCase):
    def test_forbidden_has_actionable_diagnostic_without_connection_tokens(self):
        core = import_shorts_core_with_fakes()

        class Forbidden(Exception):
            status = 403

        engine = core.EdgeTTSEngine(core.PipelineConfig(gemini_api_key="", pexels_api_key=""))
        with tempfile.TemporaryDirectory() as folder, patch.object(
            engine, "_save_async", side_effect=Forbidden("wss://provider/?TrustedClientToken=private-token")
        ), patch("importlib.metadata.version", return_value="7.2.8"):
            with self.assertRaises(core.PipelineError) as raised:
                engine.synthesize("Test narration", Path(folder))
        message = str(raised.exception)
        self.assertIn("HTTP 403", message)
        self.assertIn("7.2.8", message)
        self.assertIn("-m pip install", message)
        self.assertNotIn("private-token", message)
        self.assertNotIn("wss://", message)

    def test_other_errors_are_preserved(self):
        core = import_shorts_core_with_fakes()
        engine = core.EdgeTTSEngine(core.PipelineConfig(gemini_api_key="", pexels_api_key=""))
        with tempfile.TemporaryDirectory() as folder, patch.object(
            engine, "_save_async", side_effect=RuntimeError("Network timeout")
        ):
            with self.assertRaisesRegex(RuntimeError, "Network timeout"):
                engine.synthesize("Test narration", Path(folder))
