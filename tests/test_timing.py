import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TEMP_PARENT = Path(os.environ.get("TMP") or ROOT / "_TEMP")
sys.path.insert(0, str(ROOT))

import providers
from server import Application
from storage import Store

VID = "jNQXAC9IVRw"


class TimingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="timing-history-", dir=TEMP_PARENT))
        self.store = Store(self.root)

    def tearDown(self):
        self.store.database.close()
        shutil.rmtree(self.root, ignore_errors=False)

    def test_timing_records_are_atomic_and_cap_each_kind_provider_pair_at_fifty(self):
        for index in range(56):
            self.store.record_timing("gemini_clip", "gemini", "m", 1200, index + 1)
        for index in range(53):
            self.store.record_timing("fetch", "supadata", "", 1, index + 1)
        self.store.record_timing("fetch", "fetchtranscript", "", 1, 7)
        saved = json.loads(self.store.timing_path.read_text(encoding="utf-8"))
        self.assertEqual((saved["seeded"], len(saved["records"])), (True, 101))
        gemini = [item for item in saved["records"] if (item["kind"], item["provider"]) == ("gemini_clip", "gemini")]
        supadata = [item for item in saved["records"] if (item["kind"], item["provider"]) == ("fetch", "supadata")]
        fetchtranscript = [item for item in saved["records"] if (item["kind"], item["provider"]) == ("fetch", "fetchtranscript")]
        self.assertEqual((len(gemini), gemini[0]["elapsed_seconds"]), (50, 7))
        self.assertEqual((len(supadata), supadata[0]["elapsed_seconds"]), (50, 4))
        self.assertEqual(len(fetchtranscript), 1)
        self.assertFalse(self.store.timing_path.with_suffix(".tmp").exists())

    def test_history_seeds_from_loaded_sources_ai_receipts_and_does_not_reseed(self):
        source_text = "source text with twenty four chars"
        self.store.put("video", "fixture", {
            "id": "fixture", "updated": "2026-01-01T00:00:00+00:00",
            "sources": {
                "gemini": {"provider": "gemini", "model": "gemini-m", "elapsed_seconds": 20,
                           "last_timestamp": 3000, "clips": 2, "video_duration_seconds": 3000,
                           "raw_response": {"clips": [{"duration_seconds": 1200, "model": "gemini-m"},
                                                        {"duration_seconds": 1800, "model": "gemini-m"}]}},
                "supadata": {"provider": "supadata", "model": "", "elapsed_seconds": 5,
                             "last_timestamp": 4, "raw_response": {}},
                "antigravity": {"provider": "antigravity", "model": "agy-m", "elapsed_seconds": 30,
                                "last_timestamp": 600, "raw_response": {"duration_seconds": 600}},
                "fixture-source": {"provider": "fetchtranscript", "hash": "source-hash",
                                    "timed_text": source_text, "elapsed_seconds": 2, "last_timestamp": 4},
            },
            "outputs": {"summary": {"provider": "codex", "model": "codex-m", "parts": 2,
                                     "part_sizes": [12, 20], "combined_characters": 7,
                                     "source_characters": len(source_text),
                                     "source_hash": "source-hash", "text": "final",
                                     "receipts": [{"provider": "codex", "requested_model": "codex-m", "elapsed_seconds": 4},
                                                  {"provider": "codex", "requested_model": "codex-m", "elapsed_seconds": 8},
                                                  {"provider": "codex", "requested_model": "codex-m", "elapsed_seconds": 3}]}}
        })
        self.store.database.close()
        self.store.timing_path.unlink()
        self.store = Store(self.root)
        records = self.store.timing_records
        self.assertEqual(len([r for r in records if r["kind"] == "gemini_clip"]), 2)
        self.assertEqual([(r["size"], r["elapsed_seconds"]) for r in records if r["kind"] == "gemini_clip"],
                         [(1200, 10), (1800, 10)])
        self.assertEqual([(r["provider"], r["size"]) for r in records if r["kind"] == "fetch"],
                         [("supadata", 1), ("antigravity", 600), ("fetchtranscript", 1)])
        self.assertEqual(len([r for r in records if r["kind"] == "ai_part"]), 2)
        self.assertEqual([(r["size"], r["task"]) for r in records if r["kind"] == "ai_part"],
                         [(12, "summary"), (20, "summary")])
        combines = [r for r in records if r["kind"] == "ai_combine"]
        self.assertEqual([(r["size"], r["task"]) for r in combines], [(7, "summary")])
        before = list(records)
        self.store.database.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.timing_records, before)

    def test_gemini_clips_seed_known_lengths_without_video_total_and_skip_unknown(self):
        self.store.put("video", "clip-lengths", {
            "id": "clip-lengths",
            "sources": {
                "gemini": {"provider": "gemini", "model": "gemini-m", "elapsed_seconds": 30,
                           "clips": 3, "last_timestamp": 9000,
                           "raw_response": {"clips": [
                               {"duration_seconds": 12.5},
                               {"start": 20, "end": 36.5},
                               {},
                           ]}},
                "gemini-single": {"provider": "gemini", "model": "gemini-m", "elapsed_seconds": 8,
                                  "clips": 1, "video_duration_seconds": 600,
                                  "raw_response": {"clips": [{}]}},
            },
        })
        self.store.database.close()
        self.store.timing_path.unlink()
        self.store = Store(self.root)

        seeded = [record for record in self.store.timing_records if record["kind"] == "gemini_clip"]
        self.assertEqual([record["size"] for record in seeded], [12.5, 16.5, 600])
        self.assertEqual([record["elapsed_seconds"] for record in seeded], [10, 10, 8])

    def test_malformed_library_timing_fields_are_skipped_and_app_starts(self):
        self.store.put("video", "malformed", {
            "id": "malformed", "sources": {
                "gemini": {"provider": "gemini", "elapsed_seconds": "not-a-number", "clips": "bad",
                           "last_timestamp": "bad", "raw_response": {"video_duration_seconds": 600,
                               "clips": [{"start": "bad", "end": {}, "duration_seconds": "bad"}]}},
                "gemini-range": {"provider": "gemini", "elapsed_seconds": "bad", "clips": 1,
                                 "last_timestamp": "bad", "raw_response": {"video_duration_seconds": 600,
                                     "clips": [{"start": "bad", "end": {}, "duration_seconds": "bad"}]}},
                "antigravity": {"provider": "antigravity", "elapsed_seconds": "bad",
                                "last_timestamp": "bad", "raw_response": {"duration_seconds": "bad"}},
                "antigravity-unknown": {"provider": "antigravity", "model": "agy-m",
                                        "elapsed_seconds": 30, "raw_response": {}},
                "gemini-unknown": {"provider": "gemini", "model": "gemini-m", "elapsed_seconds": 10,
                                   "clips": 1, "last_timestamp": 600,
                                   "raw_response": {"clips": [{}]}},
                "supadata": {"provider": "supadata", "elapsed_seconds": 2},
                "not-a-record": "not a dict",
            },
            "outputs": {"summary": {"provider": "codex", "model": "codex-m", "parts": "bad",
                                      "source_characters": 10,
                                      "receipts": [{"provider": "codex", "requested_model": "codex-m",
                                                    "elapsed_seconds": 3}]}}
        })
        self.store.database.close()
        self.store.timing_path.unlink()
        self.store = Store(self.root)
        self.assertEqual([(item["kind"], item["provider"]) for item in self.store.timing_records],
                         [("fetch", "supadata")])
        self.store.database.close()
        app = Application(self.root, start_worker=False, start_readiness_check=False,
                          antigravity_work_root=self.root / "antigravity-runtime")
        self.store = app.store
        self.assertEqual([(item["kind"], item["provider"]) for item in app.store.timing_records],
                         [("fetch", "supadata")])

    def test_legacy_ai_output_without_recoverable_input_size_is_skipped(self):
        self.store.put("video", "unrecoverable", {
            "id": "unrecoverable", "sources": {"other": {"hash": "different", "timed_text": "source"}},
            "outputs": {"formatted": {"provider": "codex", "model": "m", "parts": 1,
                                        "source_hash": "missing", "text": "final output is not input",
                                        "receipts": [{"provider": "codex", "requested_model": "m",
                                                      "elapsed_seconds": 5}]}}
        })
        self.store.database.close()
        self.store.timing_path.write_text(json.dumps({"seeded": True, "records": [{
            "kind": "ai_part", "provider": "codex", "model": "m", "size": 1, "elapsed_seconds": 5,
        }]}), encoding="utf-8")
        self.store = Store(self.root)
        self.assertFalse(any(record["kind"] in ("ai_part", "ai_combine") for record in self.store.timing_records))
        self.assertEqual(json.loads(self.store.timing_path.read_text(encoding="utf-8"))["version"], 2)

    def test_legacy_multi_part_summary_uses_average_matched_source_size(self):
        source_text = "0123456789abcdefghij"
        self.store.put("video", "legacy-summary", {
            "id": "legacy-summary",
            "sources": {"import": {"hash": "summary-source-hash", "timed_text": source_text}},
            "outputs": {"summary": {
                "provider": "codex", "model": "codex-m", "parts": 2,
                "source_hash": "summary-source-hash", "text": "final assistant output",
                "receipts": [
                    {"provider": "codex", "requested_model": "codex-m", "elapsed_seconds": 4},
                    {"provider": "codex", "requested_model": "codex-m", "elapsed_seconds": 6},
                    {"provider": "codex", "requested_model": "codex-m", "elapsed_seconds": 3},
                ],
            }},
        })
        self.store.database.close()
        self.store.timing_path.unlink()
        self.store = Store(self.root)

        parts = [record for record in self.store.timing_records if record["kind"] == "ai_part"]
        self.assertEqual([(record["size"], record["task"]) for record in parts],
                         [(len(source_text) / 2, "summary")] * 2)
        self.assertFalse(any(record["kind"] == "ai_combine" for record in self.store.timing_records))

    def test_unversioned_history_rebuilds_ai_sizes_and_preserves_non_ai_records(self):
        source_text = "x" * 6877
        retained = {"kind": "gemini_clip", "provider": "gemini", "model": "gemini-m",
                    "size": 1200, "elapsed_seconds": 6, "fixture_note": "preserve exactly"}
        self.store.put("video", "formatted-legacy", {
            "id": "formatted-legacy", "sources": {"import": {"hash": "known-hash", "timed_text": source_text}},
            "outputs": {"formatted": {"provider": "codex", "model": "codex-m", "parts": 1,
                                        "source_hash": "known-hash", "source_characters": len(source_text),
                                        "receipts": [{"provider": "codex", "requested_model": "codex-m",
                                                      "elapsed_seconds": 10}]}}
        })
        self.store.database.close()
        self.store.timing_path.write_text(json.dumps({"seeded": True, "records": [
            {"kind": "ai_part", "provider": "codex", "model": "codex-m", "size": 1, "elapsed_seconds": 3600},
            {"kind": [], "provider": "broken", "model": "", "size": 1, "elapsed_seconds": 2},
            retained,
        ]}), encoding="utf-8")
        self.store = Store(self.root)
        self.assertAlmostEqual(self.store.estimate_seconds("ai_part", "codex", "codex-m", 6877,
                                                          task="formatted"), 10)
        self.assertEqual([record for record in self.store.timing_records if record["kind"] == "gemini_clip"],
                         [retained])
        self.assertFalse(any(record.get("size") == 1 for record in self.store.timing_records
                             if record["kind"] in ("ai_part", "ai_combine")))
        saved = json.loads(self.store.timing_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual([record for record in saved["records"] if record["kind"] == "gemini_clip"], [retained])

    def test_ai_history_is_separate_by_task_and_provider_with_task_local_model_preference(self):
        for _ in range(3):
            self.store.record_timing("ai_part", "codex", "same-model", 10, 20, task="formatted")
        self.store.record_timing("ai_part", "codex", "same-model", 10, 40, task="summary")
        self.store.record_timing("ai_part", "codex", "other-model", 10, 80, task="summary")
        self.store.record_timing("ai_part", "codex", "same-model", 10, 120, task="ask")
        self.store.record_timing("ai_part", "claude", "same-model", 10, 200, task="summary")
        self.assertEqual(self.store.estimate_seconds("ai_part", "codex", "same-model", 10,
                                                     task="formatted"), 20)
        self.assertEqual(self.store.estimate_seconds("ai_part", "codex", "same-model", 10,
                                                     task="summary"), 60)
        self.assertEqual(self.store.estimate_seconds("ai_part", "codex", "same-model", 10,
                                                     task="ask"), 120)
        self.assertEqual(self.store.estimate_seconds("ai_part", "claude", "same-model", 10,
                                                     task="summary"), 200)
        self.assertIsNone(self.store.estimate_seconds("ai_part", "codex", "same-model", 10))

    def test_application_starts_with_empty_history_if_entire_seed_fails(self):
        self.store.database.close()
        self.store.timing_path.unlink()
        with mock.patch.object(Store, "_seed_timing_history", side_effect=RuntimeError("fixture seed failure")):
            app = Application(self.root, start_worker=False, start_readiness_check=False,
                              antigravity_work_root=self.root / "antigravity-runtime")
        self.store = app.store
        self.assertEqual(app.store.timing_records, [])
        self.assertTrue(app.store.timing_seeded)

    def test_antigravity_size_one_is_not_saved_or_loaded_as_video_seconds(self):
        self.assertIsNone(self.store.record_timing("fetch", "antigravity", "agy-m", 1, 9))
        self.store.database.close()
        self.store.timing_path.write_text(json.dumps({"seeded": True, "records": [{
            "kind": "fetch", "provider": "antigravity", "model": "agy-m", "size": 1,
            "elapsed_seconds": 9,
        }]}), encoding="utf-8")
        self.store = Store(self.root)
        self.assertFalse(any(record["provider"] == "antigravity" for record in self.store.timing_records))

    def test_estimator_uses_median_and_requires_three_same_model_records_to_filter(self):
        for model, rate in (("m1", 2), ("m2", 4), ("m3", 6)):
            self.store.record_timing("ai_part", "median", model, 10, rate * 10, task="formatted")
        self.assertEqual(self.store.estimate_seconds("ai_part", "median", "m1", 10, task="formatted"), 40)
        for model, rate in (("m1", 2), ("m1", 4), ("m2", 6), ("m3", 100), ("m4", 100),
                            ("m5", 100), ("m6", 100), ("m7", 100), ("m8", 100), ("m9", 100)):
            self.store.record_timing("ai_part", "codex", model, 10, rate * 10, task="formatted")
        self.assertEqual(self.store.estimate_seconds("ai_part", "codex", "m1", 10, task="formatted"), 1000)
        for model, rate in (("m1", 2), ("m1", 4), ("m1", 8), ("other", 100), ("other", 100),
                            ("other", 100), ("other", 100), ("other", 100), ("other", 100),
                            ("other", 100)):
            self.store.record_timing("ai_part", "claude", model, 10, rate * 10, task="formatted")
        self.assertEqual(self.store.estimate_seconds("ai_part", "claude", "m1", 10, task="formatted"), 40)
        self.assertIsNone(self.store.estimate_seconds("fetch", "antigravity", "", 1))


class TimingJobTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="timing-jobs-", dir=TEMP_PARENT))
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.store.database.close()
        shutil.rmtree(self.root, ignore_errors=False)

    def app(self, fetcher=None, runner=None):
        data = self.root / f"app-{len(self.apps)}"
        data.mkdir()
        app = Application(data, fetcher=fetcher, ai_runner=runner, start_worker=False,
                          antigravity_work_root=data / "antigravity-runtime", start_readiness_check=False)
        self.apps.append(app)
        return app

    @staticmethod
    def result(provider, vid, **extra):
        if provider == "supadata":
            payload = {"lang": "en", "metadata": {"title": "Fixture"},
                       "content": [{"text": "Fixture transcript.", "offset": 0, "duration": 2000}]}
        else:
            payload = {"video_id": vid, "language": "en", "metadata": {"title": "Fixture"},
                       "segments": [{"text": "Fixture transcript.", "start": 0, "duration": 2}]}
        source = providers.normalize("fetchtranscript" if provider == "antigravity" else provider, payload, vid)
        source["provider"] = provider
        source.update(elapsed_seconds=0.01, **extra)
        return source

    def test_gemini_clip_fields_unknown_length_and_no_history(self):
        seen = []
        def fetcher(provider, vid, key, language, **options):
            options["step_started"](1, 3, 1200, "gemini-m", [1200, 500])
            seen.append((self.app_under_test.store.get("job", self.job_id), options))
            options["step_completed"]("gemini-m", 1200, 8)
            return self.result("gemini", vid, model="gemini-m", clips=3, last_timestamp=2900)
        self.app_under_test = self.app(fetcher=fetcher)
        app = self.app_under_test
        app.store.record_timing("gemini_clip", "gemini", "gemini-m", 1200, 10)
        app.store.record_timing("gemini_clip", "gemini", "gemini-m", 500, 5)
        app.store.save_settings({"primary": "gemini", "fallback": "none"})
        self.job_id = app.add_batch(VID)["jobs"][0]
        app.last_call["gemini"] = time.monotonic()
        job = app.store.get("job", self.job_id)
        app.fetch_job(job, threading.Event())
        current = app.store.get("job", self.job_id)
        self.assertEqual((current["step_index"], current["steps_total"], current["step_expected_seconds"]), (1, 3, 11))
        self.assertAlmostEqual(current["expected_remaining_seconds"], 11 + 11 * 500 / 1200)
        self.assertIsNotNone(current["step_started"])
        self.assertTrue(any(r["kind"] == "gemini_clip" and r["elapsed_seconds"] == 8
                            for r in app.store.timing_records))

        def unknown_fetcher(provider, vid, key, language, **options):
            options["step_started"](2, None, None, "gemini-m", [])
            options["step_completed"]("gemini-m", None, 6)
            return self.result("gemini", vid, model="gemini-m", clips=1, last_timestamp=600)
        unknown = self.app(fetcher=unknown_fetcher)
        unknown.store.record_timing("gemini_clip", "gemini", "gemini-m", 600, 6)
        unknown.store.save_settings({"primary": "gemini", "fallback": "none"})
        unknown_job = unknown.add_batch(VID)["jobs"][0]
        unknown.last_call["gemini"] = time.monotonic()
        unknown.fetch_job(unknown.store.get("job", unknown_job), threading.Event())
        unknown_saved = unknown.store.get("job", unknown_job)
        self.assertIsNone(unknown_saved["steps_total"])
        self.assertIsNone(unknown_saved["step_size"])
        self.assertIsNone(unknown_saved["step_expected_seconds"])
        self.assertEqual(len([r for r in unknown.store.timing_records if r["kind"] == "gemini_clip"]), 1)

        no_history = self.app(fetcher=lambda provider, vid, key, language, **options: (
            options["step_started"](1, 2, 600, "gemini-m", [600]),
            self.result("gemini", vid, model="gemini-m", clips=2, last_timestamp=1200))[1])
        no_history.store.save_settings({"primary": "gemini", "fallback": "none"})
        no_history_job = no_history.add_batch(VID)["jobs"][0]
        no_history.last_call["gemini"] = time.monotonic()
        no_history.fetch_job(no_history.store.get("job", no_history_job), threading.Event())
        self.assertIsNone(no_history.store.get("job", no_history_job)["step_expected_seconds"])

    def test_fixed_size_provider_steps_and_antigravity_size_update(self):
        for provider, fallback in (("supadata", "fetchtranscript"), ("fetchtranscript", "none")):
            def fetcher(name, vid, key, language, **options):
                return self.result(name, vid, model="")
            app = self.app(fetcher=fetcher)
            app.store.record_timing("fetch", provider, "", 1, 4)
            if fallback != "none":
                app.store.record_timing("fetch", fallback, "", 1, 6)
            app.store.save_settings({"primary": provider, "fallback": fallback})
            job_id = app.add_batch(VID)["jobs"][0]
            app.last_call[provider] = time.monotonic()
            app.fetch_job(app.store.get("job", job_id), threading.Event())
            job = app.store.get("job", job_id)
            self.assertEqual((job["step_index"], job["steps_total"], job["step_expected_seconds"]),
                             (1, 1, 4),
                             msg=f"{provider}: {job}")
            self.assertEqual(job["expected_remaining_seconds"], 0)

        def agy_fetcher(provider, vid, key, language, **options):
            options["step_update"](321, "agy-m")
            return self.result("antigravity", vid, model="agy-m", last_timestamp=321,
                               raw_response={"duration_seconds": 321})
        agy = self.app(fetcher=agy_fetcher)
        agy.store.record_timing("fetch", "antigravity", "agy-m", 321, 9)
        agy.store.save_settings({"primary": "antigravity", "fallback": "none"})
        job_id = agy.add_batch(VID)["jobs"][0]
        agy.last_call["antigravity"] = time.monotonic()
        agy.fetch_job(agy.store.get("job", job_id), threading.Event())
        job = agy.store.get("job", job_id)
        self.assertEqual((job["step_size"], job["step_expected_seconds"]), (321, 9))
        agy_record = next(r for r in reversed(agy.store.timing_records)
                          if r["kind"] == "fetch" and r["provider"] == "antigravity")
        self.assertEqual(agy_record["size"], 321)
        self.assertGreater(agy_record["elapsed_seconds"], 0)

        def unknown_agy_fetcher(provider, vid, key, language, **options):
            current = no_length.store.get("job", no_length_job)
            no_length_state.append((current["step_size"], current["step_expected_seconds"]))
            source = self.result("antigravity", vid, model="agy-m")
            source["last_timestamp"] = None
            source["raw_response"] = {}
            return source
        no_length_state = []
        no_length = self.app(fetcher=unknown_agy_fetcher)
        self.assertIsNone(no_length.store.record_timing("fetch", "antigravity", "agy-m", 1, 9))
        no_length.store.save_settings({"primary": "antigravity", "fallback": "none"})
        no_length_job = no_length.add_batch(VID)["jobs"][0]
        no_length.last_call["antigravity"] = time.monotonic()
        no_length.fetch_job(no_length.store.get("job", no_length_job), threading.Event())
        self.assertEqual(no_length_state, [(None, None)])
        self.assertFalse(any(r["provider"] == "antigravity" for r in no_length.store.timing_records))

    def test_fallback_attempts_are_single_steps_and_compare_counts_every_service(self):
        fallback_states = []
        fallback = self.app()
        fallback.store.keys.update(supadata="fixture-key", fetchtranscript="fixture-key")
        fallback.store.record_timing("fetch", "supadata", "", 1, 4)
        fallback.store.record_timing("fetch", "fetchtranscript", "", 1, 6)
        fallback.store.save_settings({"primary": "supadata", "fallback": "fetchtranscript"})

        def failing_primary(provider, vid, key, language, **options):
            current = fallback.store.get("job", fallback_job)
            fallback_states.append((provider, current["step_index"], current["steps_total"],
                                   current["step_expected_seconds"], current["expected_remaining_seconds"],
                                   current.get("progress_floor", 0)))
            if provider == "supadata":
                fallback.store.update("job", fallback_job, step_started="2000-01-01T00:00:00+00:00")
                raise providers.ProviderError("fixture_failure", "fixture failure")
            return self.result(provider, vid)

        fallback.fetcher = failing_primary
        fallback_job = fallback.add_batch(VID)["jobs"][0]
        fallback.last_call.update(supadata=time.monotonic(), fetchtranscript=time.monotonic())
        fallback.fetch_job(fallback.store.get("job", fallback_job), threading.Event())
        self.assertEqual([(row[0], row[1:5]) for row in fallback_states],
                         [("supadata", (1, 1, 4, 0)), ("fetchtranscript", (1, 1, 6, 0))])
        self.assertEqual(fallback_states[1][5], 95)

        compare_states = []
        compare = self.app()
        compare.store.keys.update(supadata="fixture-key", fetchtranscript="fixture-key")
        compare.store.record_timing("fetch", "supadata", "", 1, 4)
        compare.store.record_timing("fetch", "fetchtranscript", "", 1, 6)
        compare.store.record_timing("fetch", "antigravity", "agy-m", 321, 9)
        compare.store.save_settings({"primary": "supadata", "fallback": "none"})
        compare_job = compare.add_batch(VID, compare=True)["jobs"][0]
        video = compare.store.get("video", VID)
        video["sources"]["antigravity"] = {"provider": "antigravity", "model": "agy-m", "last_timestamp": 321,
                                             "raw_response": {"duration_seconds": 321}}
        compare.store.put("video", VID, video)

        def all_compare_services(provider, vid, key, language, **options):
            current = compare.store.get("job", compare_job)
            compare_states.append((provider, current["step_index"], current["steps_total"],
                                   current["step_expected_seconds"], current["expected_remaining_seconds"]))
            if provider == "antigravity":
                return self.result(provider, vid, model="agy-m", last_timestamp=321,
                                   raw_response={"duration_seconds": 321})
            return self.result(provider, vid)

        compare.fetcher = all_compare_services
        compare.last_call.update({provider: time.monotonic()
                                  for provider in ("supadata", "fetchtranscript", "antigravity")})
        compare.fetch_job(compare.store.get("job", compare_job), threading.Event())
        self.assertEqual(compare_states, [
            ("supadata", 1, 3, 4, 15),
            ("fetchtranscript", 2, 3, 6, 9),
            ("antigravity", 3, 3, 9, 0),
        ])

    def test_compare_gemini_clips_expand_the_service_total_and_keep_remaining_estimates(self):
        compare = self.app()
        compare.store.keys.update(gemini="fixture-key", supadata="fixture-key", fetchtranscript="fixture-key")
        compare.store.save_settings({"primary": "supadata", "fallback": "none"})
        compare.store.record_timing("fetch", "supadata", "", 1, 4)
        compare.store.record_timing("gemini_clip", "gemini", "gemini-m", 600, 6)
        compare.store.record_timing("fetch", "fetchtranscript", "", 1, 6)
        compare.store.record_timing("fetch", "antigravity", "agy-m", 321, 9)
        compare_job = compare.add_batch(VID, compare=True)["jobs"][0]
        video = compare.store.get("video", VID)
        video["sources"]["antigravity"] = {"provider": "antigravity", "model": "agy-m", "last_timestamp": 321,
                                             "raw_response": {"duration_seconds": 321}}
        compare.store.put("video", VID, video)
        states = []

        def compare_with_gemini(provider, vid, key, language, **options):
            def capture():
                current = compare.store.get("job", compare_job)
                states.append((provider, current["step_index"], current["steps_total"],
                               current["step_expected_seconds"], current["expected_remaining_seconds"]))
            if provider == "gemini":
                options["step_started"](1, 2, 600, "gemini-m", [600])
                capture()
                options["step_completed"]("gemini-m", 600, 6)
                options["step_started"](2, 2, 600, "gemini-m", [])
                capture()
                options["step_completed"]("gemini-m", 600, 6)
                return self.result("gemini", vid, model="gemini-m", clips=2, last_timestamp=1200)
            capture()
            if provider == "antigravity":
                return self.result(provider, vid, model="agy-m", last_timestamp=321,
                                   raw_response={"duration_seconds": 321})
            return self.result(provider, vid)

        compare.fetcher = compare_with_gemini
        compare.last_call.update({provider: time.monotonic()
                                  for provider in ("gemini", "supadata", "fetchtranscript", "antigravity")})
        compare.fetch_job(compare.store.get("job", compare_job), threading.Event())
        self.assertEqual(states, [
            ("supadata", 1, 4, 4, 15),
            ("gemini", 2, 5, 6, 21),
            ("gemini", 3, 5, 6, 15),
            ("fetchtranscript", 4, 5, 6, 9),
            ("antigravity", 5, 5, 9, 0),
        ])

    def test_server_overrun_floor_completes_only_the_current_multistep_call(self):
        self.assertEqual(Application._estimated_job_percent({
            "steps_total": 3, "step_index": 1, "step_expected_seconds": 60,
            "step_started": "2000-01-01T00:00:00+00:00",
        }), 33)
        self.assertEqual(Application._estimated_job_percent({
            "steps_total": 3, "step_index": 3, "step_expected_seconds": 30,
            "step_started": "2000-01-01T00:00:00+00:00",
        }), 95)

    def test_ai_parts_and_combining_have_character_sizes_and_history_estimates(self):
        combine_size = len("PART 1\nnotes\n\nPART 2\nnotes")
        for provider in ("codex", "claude", "antigravity", "opencode"):
            captures = []
            model = f"{provider}-m"

            def runner(name, prompt, *args, **kwargs):
                job = app.store.get("job", job_id)
                captures.append(({key: job.get(key) for key in (
                    "step_index", "steps_total", "step_expected_seconds", "expected_remaining_seconds",
                    "step_kind", "step_size", "step_task")}, kwargs.get("kind")))
                time.sleep(0.002)
                return "notes", {"provider": name, "requested_model": model, "elapsed_seconds": 1}

            app = self.app(runner=runner)
            if provider == "opencode":
                with mock.patch("storage.opencode_cli.model_is_listed", return_value=True):
                    app.store.save_settings({"ai_provider": provider, "ai_model": model})
            else:
                app.store.save_settings({"ai_provider": provider, "ai_model": model})
            video_id = app.import_text({"text": "A" * 21000})["id"]
            for size, elapsed in ((20000, 20), (20000, 20), (20000, 20),
                                  (1000, 1), (1000, 1), (1000, 1)):
                app.store.record_timing("ai_part", provider, model, size, elapsed, task="summary")
            for _ in range(3):
                app.store.record_timing("ai_part", provider, "other-model", 20000, 200, task="summary")
                app.store.record_timing("ai_combine", provider, "other-model", combine_size, 200, task="summary")
                app.store.record_timing("ai_combine", provider, model, combine_size, combine_size / 2,
                                        task="summary")
            job_id = app.submit(video_id, "summary")
            app.ai_job(app.store.get("job", job_id), threading.Event())
            self.assertEqual([(item[0]["step_index"], item[0]["steps_total"], item[0]["step_kind"],
                               item[0]["step_size"], item[0]["step_task"]) for item in captures],
                             [(1, 3, "ai_part", 20000, "summary"),
                              (2, 3, "ai_part", 1000, "summary"),
                              (3, 3, "ai_combine", combine_size, "summary")])
            self.assertEqual([item[0]["step_expected_seconds"] for item in captures], [20, 1, combine_size / 2])
            self.assertEqual([item[0]["expected_remaining_seconds"] for item in captures], [1, 0, 0])
            self.assertEqual([item[1] for item in captures], ["summary" if provider == "opencode" else None] * 3)
            matching = [r for r in app.store.timing_records if r["provider"] == provider and r["model"] == model
                        and r["task"] == "summary"]
            self.assertEqual([r["kind"] for r in matching].count("ai_part"), 8)
            self.assertEqual([r["kind"] for r in matching].count("ai_combine"), 4)
            saved = app.store.get("video", video_id)["outputs"]["summary"]
            self.assertEqual([receipt["timing_task"] for receipt in saved["receipts"]],
                             ["summary", "summary", "summary"])

    def test_claude_and_antigravity_cli_part_calls_are_recorded(self):
        for provider in ("claude", "antigravity"):
            def runner(name, prompt, *args, **kwargs):
                time.sleep(.002)
                return "formatted", {"provider": name, "requested_model": f"{name}-m", "elapsed_seconds": .01}
            app = self.app(runner=runner)
            model = f"{provider}-m"
            app.store.save_settings({"ai_provider": provider, "ai_model": model})
            source_text = "A short fixture transcript."
            video_id = app.import_text({"text": source_text})["id"]
            app.store.record_timing("ai_part", provider, model, len(source_text), .02, task="formatted")
            job_id = app.submit(video_id, "formatted")
            app.ai_job(app.store.get("job", job_id), threading.Event())
            saved = next(record for record in reversed(app.store.timing_records)
                         if record["provider"] == provider and record["kind"] == "ai_part"
                         and record["task"] == "formatted")
            self.assertEqual((saved["model"], saved["size"], saved["elapsed_seconds"]),
                             (model, len(source_text), .01))
            receipt = app.store.get("video", video_id)["outputs"]["formatted"]["receipts"][0]
            self.assertEqual(receipt["timing_task"], "formatted")


if __name__ == "__main__":
    unittest.main()
