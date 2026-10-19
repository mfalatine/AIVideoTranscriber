"""Optional interpreter selection uses environment values and file presence only."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_paths


VARIABLES = (runtime_paths.AGY_PYTHON_ENV, runtime_paths.YTDLP_PYTHON_ENV,
             runtime_paths.WHISPER_PYTHON_ENV)


class RuntimePathTests(unittest.TestCase):
    def test_explicit_environment_path_wins_for_each_optional_runtime(self):
        for variable in VARIABLES:
            with self.subTest(variable=variable), mock.patch.dict(os.environ, {variable: r"C:\chosen Python\python.exe"}):
                discover = mock.Mock(return_value=Path("automatic.exe"))
                self.assertEqual(runtime_paths.resolve_interpreter(variable, discover),
                                 Path(r"C:\chosen Python\python.exe"))
                discover.assert_not_called()

    def test_discovery_and_absence_for_each_optional_runtime(self):
        for variable in VARIABLES:
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as folder:
                candidate = Path(folder) / "python.exe"
                candidate.touch()
                with mock.patch.dict(os.environ):
                    os.environ.pop(variable, None)
                    self.assertEqual(runtime_paths.resolve_interpreter(variable, lambda: candidate), candidate)
                    self.assertIsNone(runtime_paths.resolve_interpreter(variable, lambda: None))
                    message = runtime_paths.missing_interpreter_message(variable, "Optional feature")
                    self.assertIn(variable, message)
                    self.assertIn("full path", message)

    def test_download_discovery_prefers_the_existing_legacy_python312_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "Programs" / "Python"
            preferred = root / "Python312" / "python.exe"
            preferred.parent.mkdir(parents=True)
            preferred.touch()
            other = root / "Python313" / "python.exe"
            other.parent.mkdir()
            other.touch()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                self.assertEqual(runtime_paths.discover_download_python(), preferred)

    def test_whisper_discovery_uses_the_current_user_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / "AIVideoTranscriberWhisper" / "Scripts" / "python.exe"
            candidate.parent.mkdir(parents=True)
            candidate.touch()
            with mock.patch.dict(os.environ, {"USERPROFILE": folder}):
                self.assertEqual(runtime_paths.discover_whisper_python(), candidate)
            candidate.unlink()
            with mock.patch.dict(os.environ, {"USERPROFILE": folder}):
                self.assertIsNone(runtime_paths.discover_whisper_python())


if __name__ == "__main__":
    unittest.main()
