"""Retry and local-model-path behavior for the isolated Whisper worker."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import whisper_worker


class WhisperWorkerAvCompatibilityTests(unittest.TestCase):
    def install(self, opener):
        av = SimpleNamespace(open=opener)
        with mock.patch.dict(sys.modules, {"av": av}):
            whisper_worker.install_av_open_compatibility()
            installed = av.open
            whisper_worker.install_av_open_compatibility()
            self.assertIs(av.open, installed)
        return av.open

    def test_retries_without_only_the_unsupported_metadata_keyword(self):
        calls = []
        expected = object()

        def opener(*args, **kwargs):
            calls.append((args, kwargs))
            if "metadata_errors" in kwargs:
                raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")
            return expected

        compatible = self.install(opener)
        options = {"mode": "r", "metadata_errors": "ignore", "options": {"threads": "1"}}
        self.assertIs(compatible("clip.mp4", **options), expected)
        self.assertEqual(calls, [(("clip.mp4",), options),
                                 (("clip.mp4",), {"mode": "r", "options": {"threads": "1"}})])
        self.assertEqual(options["metadata_errors"], "ignore")

    def test_supported_keyword_is_forwarded_without_retry(self):
        opener = mock.Mock(return_value=object())
        compatible = self.install(opener)
        self.assertIs(compatible("clip.mp4", metadata_errors="ignore"), opener.return_value)
        opener.assert_called_once_with("clip.mp4", metadata_errors="ignore")

    def test_unrelated_errors_and_calls_without_keyword_propagate(self):
        unsupported = TypeError("open() got an unexpected keyword argument 'metadata_errors'")
        for error, kwargs in (
                (TypeError("invalid input type for metadata_errors"), {"metadata_errors": "ignore"}),
                (TypeError("open() got an unexpected keyword argument 'mode'"), {"metadata_errors": "ignore"}),
                (ValueError("invalid media"), {"metadata_errors": "ignore"}),
                (unsupported, {})):
            with self.subTest(error=str(error), kwargs=kwargs):
                opener = mock.Mock(side_effect=error)
                compatible = self.install(opener)
                with self.assertRaises(type(error)) as raised:
                    compatible("clip.mp4", **kwargs)
                self.assertIs(raised.exception, error)
                opener.assert_called_once_with("clip.mp4", **kwargs)

    def test_retry_error_is_preserved(self):
        error = TypeError("invalid mode")
        opener = mock.Mock(side_effect=[
            TypeError("open() got an unexpected keyword argument 'metadata_errors'"), error])
        compatible = self.install(opener)
        with self.assertRaises(TypeError) as raised:
            compatible("clip.mp4", metadata_errors="ignore")
        self.assertIs(raised.exception, error)
        self.assertEqual(opener.call_count, 2)

    def test_runtime_installs_compatibility_before_importing_faster_whisper(self):
        opener = mock.Mock()
        av = SimpleNamespace(open=opener)

        class FasterWhisper:
            @property
            def WhisperModel(inner):
                self.assertIsNot(av.open, opener)
                return "model"

        downloader = object()
        with mock.patch.dict(sys.modules, {
                "av": av, "faster_whisper": FasterWhisper(),
                "faster_whisper.utils": SimpleNamespace(download_model=downloader),
                "ctranslate2": SimpleNamespace(get_cuda_device_count=lambda: 1)}), \
             mock.patch.object(whisper_worker, "add_dll_directories"):
            self.assertEqual(whisper_worker.load_runtime(), ("model", downloader, 1))


class WhisperWorkerDownloadTests(unittest.TestCase):
    def test_model_and_nvidia_directories_follow_the_running_venv(self):
        prefix = Path(sys.prefix)
        self.assertEqual(whisper_worker.VENV_PYTHON, Path(sys.executable))
        self.assertEqual(whisper_worker.MODEL_ROOT, prefix / "models")
        self.assertTrue(all(path.is_relative_to(prefix) for path in whisper_worker.DLL_DIRECTORIES))

    def test_connection_retries_resume_partial_cache_and_return_downloaded_path(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "models"
            partial = cache / "partial-download.bin"
            cancel = Path(folder) / "cancel"
            clock = [0.0]
            starts = []
            sleeps = []
            progress = []

            def fake_sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds

            def fake_download(model_name, cache_dir):
                self.assertEqual(model_name, "large-v3")
                self.assertEqual(cache_dir, str(cache))
                starts.append(clock[0])
                cache.mkdir(parents=True, exist_ok=True)
                if len(starts) == 1:
                    partial.write_bytes(b"resumable bytes")
                else:
                    self.assertEqual(partial.read_bytes(), b"resumable bytes")
                if len(starts) < whisper_worker.MODEL_DOWNLOAD_TRIES:
                    raise ConnectionError("connection reset")
                return str(cache / "snapshot")

            with mock.patch.object(whisper_worker, "MODEL_ROOT", cache), \
                 mock.patch.object(whisper_worker, "emit", side_effect=progress.append):
                result = whisper_worker.download_model_with_retry(
                    fake_download, cancel, sleep=fake_sleep, monotonic=lambda: clock[0])

            self.assertEqual(result, str(cache / "snapshot"))
            self.assertEqual(len(starts), 10)
            self.assertEqual(starts, [0, 2, 6, 14, 29, 59, 89, 119, 149, 179])
            self.assertEqual(sum(sleeps), 179)
            self.assertEqual(partial.read_bytes(), b"resumable bytes")
            messages = [item["message"] for item in progress]
            self.assertEqual(len(messages), 10)
            self.assertEqual(messages[0],
                             "Downloading the Whisper voice model (about 2.9 GB, first time only) · try 1 of 10")
            self.assertIn("try 3 of 10 after a dropped connection", messages[2])
            self.assertIn("try 10 of 10 after a dropped connection", messages[-1])

    def test_non_connection_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            errors = (ValueError("invalid local model configuration"),
                      urllib.error.HTTPError("https://model.invalid/file", 404, "not found", {}, None))
            for error in errors:
                with self.subTest(error=type(error).__name__):
                    calls = []
                    progress = []

                    def fake_download(*args, **kwargs):
                        calls.append((args, kwargs))
                        raise error

                    with mock.patch.object(whisper_worker, "emit", side_effect=progress.append):
                        with self.assertRaises(type(error)):
                            whisper_worker.download_model_with_retry(fake_download, Path(folder) / "cancel")
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(len(progress), 1)

    def test_cancel_during_backoff_prevents_the_next_download_try(self):
        with tempfile.TemporaryDirectory() as folder:
            cancel = Path(folder) / "cancel"
            clock = [0.0]
            calls = []
            sleeps = []
            progress = []

            def fake_sleep(seconds):
                sleeps.append(seconds)
                clock[0] += seconds
                cancel.touch()

            def fake_download(*args, **kwargs):
                calls.append((args, kwargs))
                raise ConnectionError("connection reset")

            with mock.patch.object(whisper_worker, "emit", side_effect=progress.append):
                result = whisper_worker.download_model_with_retry(
                    fake_download, cancel, sleep=fake_sleep, monotonic=lambda: clock[0])
            self.assertIsNone(result)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sum(sleeps), .25)
            self.assertEqual(progress[-1], {"type": "canceled"})

    def test_wrapped_windows_connection_reset_is_retryable(self):
        wrapped = RuntimeError(
            "An error happened while locating files; Got: ConnectError: [WinError 10054]")
        self.assertTrue(whisper_worker.is_connection_error(wrapped))
        self.assertTrue(whisper_worker.is_connection_error(
            urllib.error.URLError(ConnectionError("connection reset"))))
        self.assertFalse(whisper_worker.is_connection_error(ValueError("bad model id")))

    def test_local_cache_lookup_uses_complete_snapshot_without_retry_download(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot"
            snapshot.mkdir()
            for name in whisper_worker.MODEL_REQUIRED_FILES:
                (snapshot / name).touch()
            downloader = mock.Mock(return_value=str(snapshot))
            output = []

            with mock.patch.object(whisper_worker, "MODEL_ROOT", Path(folder) / "models"), \
                 mock.patch.object(whisper_worker, "download_model_with_retry") as retry, \
                 mock.patch.object(whisper_worker, "emit", side_effect=output.append):
                result = whisper_worker.resolve_model_path(downloader, Path(folder) / "cancel")

            self.assertEqual(result, str(snapshot))
            downloader.assert_called_once_with(
                "large-v3", cache_dir=str(Path(folder) / "models"), local_files_only=True)
            retry.assert_not_called()
            self.assertEqual(output, [{"type": "progress", "message": "Loading the Whisper voice model"}])

    def test_local_cache_missing_required_file_falls_through_to_retry_download(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot"
            snapshot.mkdir()
            for name in whisper_worker.MODEL_REQUIRED_FILES[:-1]:
                (snapshot / name).touch()
            downloaded = Path(folder) / "downloaded-snapshot"
            downloader = mock.Mock(side_effect=[str(snapshot), str(downloaded)])
            output = []

            with mock.patch.object(whisper_worker, "MODEL_ROOT", Path(folder) / "models"), \
                 mock.patch.object(whisper_worker, "emit", side_effect=output.append):
                result = whisper_worker.resolve_model_path(downloader, Path(folder) / "cancel")

            self.assertEqual(result, str(downloaded))
            self.assertEqual(downloader.call_args_list, [
                mock.call("large-v3", cache_dir=str(Path(folder) / "models"), local_files_only=True),
                mock.call("large-v3", cache_dir=str(Path(folder) / "models")),
            ])
            self.assertEqual(output, [{
                "type": "progress",
                "message": "Downloading the Whisper voice model (about 2.9 GB, first time only) · try 1 of 10",
            }])

    def test_local_cache_lookup_exception_falls_through_to_retry_download(self):
        with tempfile.TemporaryDirectory() as folder:
            downloaded = Path(folder) / "downloaded-snapshot"

            def lookup(model_name, **options):
                if options.get("local_files_only"):
                    raise OSError("local cache lookup failed")
                return str(downloaded)

            downloader = mock.Mock(side_effect=lookup)
            output = []

            with mock.patch.object(whisper_worker, "MODEL_ROOT", Path(folder) / "models"), \
                 mock.patch.object(whisper_worker, "emit", side_effect=output.append):
                result = whisper_worker.resolve_model_path(downloader, Path(folder) / "cancel")

            self.assertEqual(result, str(downloaded))
            self.assertEqual(downloader.call_args_list, [
                mock.call("large-v3", cache_dir=str(Path(folder) / "models"), local_files_only=True),
                mock.call("large-v3", cache_dir=str(Path(folder) / "models")),
            ])
            self.assertEqual(output, [{
                "type": "progress",
                "message": "Downloading the Whisper voice model (about 2.9 GB, first time only) · try 1 of 10",
            }])

    def test_transcription_loads_complete_cached_model_path(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "clip.mp4"
            cancel = Path(folder) / "cancel"
            local_model = Path(folder) / "models" / "snapshot"
            local_model.mkdir(parents=True)
            for name in whisper_worker.MODEL_REQUIRED_FILES:
                (local_model / name).touch()
            calls = []
            output = []

            class Segment:
                start, end, text = 0.0, 1.25, "known words"

            class Info:
                duration, language, language_probability = 1.25, "en", .99

            class FakeModel:
                def __init__(self, model_path, **options):
                    calls.append((model_path, options))

                def transcribe(self, path, **options):
                    return iter([Segment()]), Info()

            fake_downloader = mock.Mock(return_value=str(local_model))
            with mock.patch.object(whisper_worker, "load_runtime",
                                   return_value=(FakeModel, fake_downloader, 1)), \
                 mock.patch.object(whisper_worker, "MODEL_ROOT", Path(folder) / "models"), \
                 mock.patch.object(whisper_worker, "download_model_with_retry",
                                   return_value=str(local_model)) as download, \
                 mock.patch.object(whisper_worker, "gpu_name", return_value="Fixture GPU"), \
                 mock.patch.object(whisper_worker, "emit", side_effect=output.append):
                status = whisper_worker.transcribe(source, cancel)

            fake_downloader.assert_called_once_with(
                "large-v3", cache_dir=str(Path(folder) / "models"), local_files_only=True)
            download.assert_not_called()
            self.assertEqual(status, 0)
            self.assertEqual(calls[0], (str(local_model), {"device": "cuda", "compute_type": "float16"}))
            result = next(item for item in output if item.get("type") == "result")
            self.assertEqual(result["model_path"], str(local_model))
            self.assertEqual(result["device"], "Fixture GPU")
            self.assertEqual(result["compute_type"], "float16")
            self.assertIn({"type": "progress", "message": "Loading the Whisper voice model"}, output)
            self.assertGreaterEqual(result["inference_elapsed_seconds"], 0)
            self.assertGreaterEqual(result["model_setup_elapsed_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
