import ast
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_cli
import app_version
import antigravity_source
import gemini
import providers
from server import Application, selected
from storage import DEFAULTS, Store

VID = 'jNQXAC9IVRw'
SUPADATA = {'content': [{'text': 'there are twelve garden beds', 'offset': 1500, 'duration': 2500}], 'lang': 'en'}
FETCH = {'video_id': VID, 'segments': [{'text': 'There are twelve garden beds.', 'start': 1.5, 'duration': 2.5}],
         'language': 'en', 'metadata': {'title': 'A garden'}, 'is_generated': True}
GEMINI = {'status': 'completed',
          'usage': {'input_tokens_by_modality': [{'modality': 'audio', 'tokens': 100}], 'total_input_tokens': 100, 'total_output_tokens': 10},
          'steps': [{'type': 'model_output', 'content': [{'type': 'text', 'text': json.dumps(
              {'language': 'en', 'segments': [{'start': 2, 'text': 'Gemini heard this.'}]})}]}]}
SERVICE_KEYS = ('GEMINI_API_KEY', 'SUPADATA_API_KEY', 'FETCHTRANSCRIPT_API_KEY')


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.body = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit):
        return self.body[:limit]


class ProviderTests(unittest.TestCase):
    def test_video_id_forms_and_duplicates(self):
        for url in [VID, f'https://youtu.be/{VID}?t=30', f'https://www.youtube.com/watch?v={VID}&list=PL1',
                    f'https://m.youtube.com/shorts/{VID}', f'https://youtube.com/live/{VID}']:
            self.assertEqual(providers.video_id(url), VID)

    def test_reject_other_hosts_and_non_videos(self):
        for value in ['https://youtube.com.evil.test/watch?v='+VID, 'http://127.0.0.1/private',
                      'file:///etc/passwd', 'https://user@youtube.com/watch?v='+VID,
                      'https://youtube.com/playlist?list=PL1']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                providers.video_id(value)

    def test_shared_link_cases_match_the_browser_copy(self):
        # static/urls.js (the Add videos preview) is checked against these same cases in tests/urls.test.js.
        cases = json.loads((Path(__file__).parent / 'video_link_cases.json').read_text(encoding='utf-8'))['links']
        self.assertGreater(len([c for c in cases if c['id']]), 10)
        self.assertGreater(len([c for c in cases if not c['id']]), 10)
        for case in cases:
            with self.subTest(value=case['input']):
                if case['id'] is None:
                    with self.assertRaises(ValueError):
                        providers.video_id(case['input'])
                else:
                    self.assertEqual(providers.video_id(case['input']), case['id'])

    def test_timing_conversion_and_raw_retention(self):
        result = providers.normalize('supadata', SUPADATA, VID)
        self.assertEqual(result['segments'][0]['start'], 1.5)
        self.assertEqual(result['segments'][0]['duration'], 2.5)
        self.assertEqual(result['raw_response'], SUPADATA)
        self.assertIn('[00:00:01]', result['timed_text'])

    def test_metadata_and_returned_identity(self):
        result = providers.normalize('fetchtranscript', FETCH, VID)
        self.assertEqual(result['title'], 'A garden')
        self.assertTrue(result['generated_captions'])
        with self.assertRaises(providers.ProviderError):
            providers.normalize('fetchtranscript', {**FETCH, 'video_id':'different00'}, VID)

    def test_empty_invalid_and_nonfinite_rejected(self):
        bad = [{'content':[]}, {'content':'plain string'}, {'content':[{'text':'hi','offset':float('nan')}]},
               {'content':[{'text':'hi','offset':-3}]}, {'content':[{'text':'hi'}]}]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(providers.ProviderError):
                providers.normalize('supadata', payload, VID)

    def test_native_only_and_auth_header(self):
        requests = []
        def opener(request, **kwargs):
            requests.append(request)
            return Response(SUPADATA)
        providers.fetch('supadata', VID, 'private-key', opener=opener)
        self.assertIn('mode=native', requests[0].full_url)
        self.assertNotIn('private-key', requests[0].full_url)
        self.assertEqual(requests[0].get_header('X-api-key'), 'private-key')
        self.assertEqual(requests[0].get_header('User-agent'), f'AIVideoTranscriber/{app_version.VERSION}')
        def fetch_opener(request, **kwargs):
            requests.append(request)
            return Response(FETCH)
        providers.fetch('fetchtranscript', VID, 'private-key', opener=fetch_opener)
        self.assertEqual(requests[1].get_header('User-agent'), f'AIVideoTranscriber/{app_version.VERSION}')

    def test_fetchtranscript_contract(self):
        requests = []
        def opener(request, **kwargs):
            requests.append(request)
            return Response(FETCH, headers={'X-Credits-Remaining':'24'})
        result = providers.fetch('fetchtranscript', VID, 'secret', opener=opener)
        self.assertIn('/v1/transcripts/'+VID, requests[0].full_url)
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer secret')
        self.assertEqual(result['credits_remaining'], '24')

    def test_missing_key_never_calls_network(self):
        with self.assertRaises(providers.ProviderError) as found:
            providers.fetch('supadata', VID, '', opener=lambda *a, **k: self.fail('network called'))
        self.assertEqual(found.exception.code, 'missing_key')

    def test_http_errors_are_actionable_and_do_not_echo_keys(self):
        for status, code in [(401,'authentication'), (402,'quota'), (404,'no_transcript'), (429,'rate_limit'), (503,'service_error')]:
            def opener(request, **kwargs):
                raise urllib.error.HTTPError(request.full_url,status,'secret-key',{},io.BytesIO(b'secret-key'))
            with self.subTest(status=status), self.assertRaises(providers.ProviderError) as found:
                providers.fetch('supadata',VID,'secret-key',opener=opener)
            self.assertEqual(found.exception.code, code)
            self.assertNotIn('secret-key', str(found.exception))

    def test_partial_and_async_are_not_success(self):
        for status in (202,206):
            with self.subTest(status=status), self.assertRaises(providers.ProviderError):
                providers.fetch('supadata', VID, 'key', opener=lambda *a, **k:Response({},status))

    def test_gemini_is_dispatched_to_its_client_and_caption_services_ignore_its_options(self):
        self.assertEqual(providers.PROVIDERS, ('gemini', 'supadata', 'fetchtranscript', 'antigravity'))
        self.assertEqual(providers.KEYED_PROVIDERS, ('gemini', 'supadata', 'fetchtranscript'))
        self.assertEqual(providers.LABELS['gemini'], 'Gemini')
        urls, messages = [], []
        def opener(request, **kwargs):
            urls.append(request.full_url)
            if request.full_url.startswith(gemini.OEMBED):
                return Response({'title': 'Dispatch title'})
            if urls.count(gemini.ENDPOINT) > 1:  # explicit semantic past-end fixture
                raise urllib.error.HTTPError(request.full_url, 400, 'Bad Request', {}, io.BytesIO(
                    json.dumps({'error': {'message': 'start_offset is beyond the video duration'}}).encode()))
            return Response(GEMINI)
        result = providers.fetch('gemini', VID, 'private-key', 'en', opener=opener, model='gemini-test-1',
                                 progress=messages.append, duration_reader=lambda vid, cancel: None)
        self.assertEqual((result['provider'], result['model'], result['title']), ('gemini', 'gemini-test-1', 'Dispatch title'))
        self.assertEqual((urls[0], result['end_detected_by']), (gemini.ENDPOINT, 'http_400'))
        self.assertEqual(messages, ['Gemini · exact YouTube duration unavailable; using clip evidence to detect the end.',
                                    'Gemini · gemini-test-1 · transcribing 00:00-20:00 (part 1)',
                                    'Gemini · gemini-test-1 · transcribing 20:00-40:00 (part 2)'])
        with self.assertRaises(providers.ProviderError) as found:
            providers.fetch('gemini', VID, '', opener=lambda *a, **k: self.fail('network called'))
        self.assertEqual(found.exception.code, 'missing_key')
        self.assertIn('Gemini', str(found.exception))
        caption = providers.fetch('supadata', VID, 'private-key', opener=lambda *a, **k: Response(SUPADATA),
                                  model='ignored', cancel=threading.Event(), progress=messages.append)
        self.assertEqual(caption['provider'], 'supadata')


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in SERVICE_KEYS:
            os.environ.pop(name, None)
        def fetcher(provider, vid, key, language, **options):
            self.calls.append(provider)
            if provider == 'gemini':
                return {**providers.normalize('fetchtranscript',{**FETCH,'video_id':vid},vid),'provider':'gemini','model':'fixture-model','clips':3,
                        'input_tokens':312000,'output_tokens':4100,'elapsed_seconds':0.01}
            payload = SUPADATA if provider == 'supadata' else FETCH
            return {**providers.normalize(provider,payload,vid),'elapsed_seconds':0.01}
        self.antigravity_work_root = Path(self.temp.name) / 'antigravity-runtime'
        self.app=Application(self.temp.name,fetcher=fetcher,start_worker=False,
                             antigravity_work_root=self.antigravity_work_root,start_readiness_check=False)
        # These tests were written for Supadata first and FetchTranscript as fallback; the app default is now Gemini.
        self.app.store.save_settings({'primary':'supadata','fallback':'fetchtranscript'})

    def tearDown(self):
        self.app.store.database.close()
        self.temp.cleanup()

    def fetch_job(self, compare=False):
        response=self.app.add_batch(VID,compare=compare)
        job=self.app.store.get('job',response['jobs'][0])
        return job

    def test_fallback_and_provider_attribution(self):
        normal=self.app.fetcher
        def fail_first(provider,*args,**options):
            if provider=='supadata':
                self.calls.append(provider)
                raise providers.ProviderError('rate_limit','Rate limited')
            return normal(provider,*args,**options)
        self.app.fetcher=fail_first
        job=self.fetch_job()
        self.app.fetch_job(job,threading.Event())
        video=self.app.store.get('video',VID)
        self.assertEqual(self.calls,['supadata','fetchtranscript'])
        self.assertEqual(video['selected_provider'],'fetchtranscript')
        self.assertEqual([a['status'] for a in video['attempts']],['failed','success'])

    def test_antigravity_source_persists_provenance_and_uses_normal_fallback(self):
        self.app.store.save_settings({'primary': 'antigravity', 'fallback': 'fetchtranscript'})
        job = self.fetch_job()
        self.app.fetch_job(job, threading.Event())
        video = self.app.store.get('video', VID)
        self.assertEqual((self.calls, video['selected_provider']), (['antigravity'], 'antigravity'))
        self.assertEqual(video['sources']['antigravity']['raw_response'], FETCH)
        self.app.store.update('job', job['id'], status='completed')
        normal = self.app.fetcher
        self.calls.clear()
        def local_failure(provider, *args, **options):
            if provider == 'antigravity':
                self.calls.append(provider)
                raise providers.ProviderError('service_error', 'Fixture local source failure')
            return normal(provider, *args, **options)
        self.app.fetcher = local_failure
        result = self.app.add_batch(VID, force=True)
        fallback = self.app.store.get('job', result['jobs'][0])
        self.app.fetch_job(fallback, threading.Event())
        saved = self.app.store.get('video', VID)
        self.assertEqual(self.calls, ['antigravity', 'fetchtranscript'])
        self.assertEqual(saved['selected_provider'], 'fetchtranscript')
        self.assertEqual([attempt['status'] for attempt in saved['attempts']], ['success', 'failed', 'success'])

    def test_unexpected_source_exception_is_sanitized_recorded_and_uses_fallback(self):
        first = self.fetch_job()
        self.app.fetch_job(first, threading.Event())
        self.app.store.update('job', first['id'], status='completed')
        video = self.app.store.get('video', VID)
        video['outputs']['summary'] = {'text': 'Keep this prior output.', 'source_hash': video['sources']['supadata']['hash']}
        self.app.store.put('video', VID, video)
        self.app.store.save_settings({'primary': 'antigravity', 'fallback': 'fetchtranscript'})
        normal = self.app.fetcher
        self.calls.clear()
        def broken_source(provider, *args, **options):
            if provider == 'antigravity':
                self.calls.append(provider)
                raise ValueError('bad float duration with fixture-private-detail')
            return normal(provider, *args, **options)
        self.app.fetcher = broken_source
        response = self.app.add_batch(VID, force=True)
        job = self.app.store.get('job', response['jobs'][0])
        self.app.fetch_job(job, threading.Event())
        video = self.app.store.get('video', VID)
        self.assertEqual((self.calls, video['selected_provider']),
                         (['antigravity', 'fetchtranscript'], 'fetchtranscript'))
        self.assertIn('supadata', video['sources'])
        self.assertEqual(video['outputs']['summary']['text'], 'Keep this prior output.')
        failed = video['attempts'][1]
        self.assertEqual((failed['status'], failed['code']), ('failed', 'provider_error'))
        self.assertIn('failed unexpectedly', failed['message'])
        self.assertNotIn('private-detail', json.dumps(video['attempts']))

    def test_interrupted_source_stops_without_attempting_the_fallback(self):
        self.app.store.save_settings({'primary': 'antigravity', 'fallback': 'fetchtranscript'})
        calls = []
        def interrupted(provider, *args, **options):
            calls.append(provider)
            raise InterruptedError('fixture cancellation')
        self.app.fetcher = interrupted
        job = self.fetch_job()
        with self.assertRaises(InterruptedError):
            self.app.fetch_job(job, threading.Event())
        self.assertEqual(calls, ['antigravity'])
        self.assertEqual(self.app.store.get('video', VID)['attempts'], [])

    def test_application_startup_sweeps_only_the_injected_antigravity_task_root(self):
        runtime = Path(self.temp.name) / 'startup-antigravity-runtime'
        stale = runtime / ('task-' + '5' * 24)
        stale.mkdir(parents=True)
        (stale / 'private.webm').write_bytes(b'fixture private audio')
        database = Path(self.temp.name) / 'startup-database'
        with mock.patch.object(antigravity_source, '_process_alive', return_value=False):
            other = Application(database, start_worker=False, antigravity_work_root=runtime,
                                start_readiness_check=False)
        try:
            self.assertFalse(runtime.exists())
            self.assertEqual(other.antigravity_work_root, runtime)
        finally:
            other.store.database.close()

    def test_comparison_retrieves_both_and_keeps_sources(self):
        self.app.store.keys.update({'supadata':'key-one','fetchtranscript':'key-two'})
        self.app.fetch_job(self.fetch_job(compare=True),threading.Event())
        video=self.app.store.get('video',VID)
        self.assertEqual(self.calls,['supadata','fetchtranscript','antigravity'])
        self.assertEqual(set(video['sources']),{'supadata','fetchtranscript','antigravity'})
        self.assertEqual(video['selected_provider'],'supadata')
        self.assertEqual(video['sources']['supadata']['raw_response'],SUPADATA)

    def test_cache_and_duplicate_input(self):
        response=self.app.add_batch(VID+'\nhttps://youtu.be/'+VID)
        self.assertEqual(len(response['jobs']),1)
        job=self.app.store.get('job',response['jobs'][0])
        self.app.fetch_job(job,threading.Event())
        self.assertEqual(self.app.add_batch(VID)['reused'],1)
        self.assertEqual(self.calls,['supadata'])

    def test_forced_retrieval_updates_one_saved_record_and_keeps_outputs(self):
        job = self.fetch_job()
        self.app.fetch_job(job, threading.Event())
        self.app.store.update('job', job['id'], status='completed')
        video = self.app.store.get('video', VID)
        video['outputs']['summary'] = {'text': 'Keep this output.', 'source_hash': video['sources']['supadata']['hash']}
        self.app.store.put('video', VID, video)
        result = self.app.add_batch(VID, mode='fetchtranscript', force=True)
        self.assertEqual((len(result['jobs']), result['reused']), (1, 0))
        forced = self.app.store.get('job', result['jobs'][0])
        self.assertTrue(forced['rerun'])
        self.app.fetch_job(forced, threading.Event())
        saved = self.app.store.get('video', VID)
        self.assertEqual(len(self.app.store.list('video')), 1)
        self.assertEqual(saved['selected_provider'], 'fetchtranscript')
        self.assertEqual(saved['outputs']['summary']['text'], 'Keep this output.')

    def test_manual_paste_does_not_block_caption_retrieval(self):
        self.app.import_text({'url': VID, 'title': 'Pasted title', 'text': 'h'})
        job = self.fetch_job()
        self.app.fetch_job(job, threading.Event())
        video = self.app.store.get('video', VID)
        self.assertEqual(self.calls, ['supadata'])
        self.assertEqual(video['selected_provider'], 'supadata')
        self.assertEqual(video['sources']['import']['text'], 'h')
        self.assertEqual(self.app.add_batch(VID)['reused'], 1)

    def test_manual_paste_can_be_corrected_for_same_url(self):
        self.app.import_text({'url': VID, 'title': 'Pasted title', 'text': 'h'})
        self.app.import_text({'url': VID, 'title': 'Pasted title', 'text': 'A complete pasted transcript.'})
        video = self.app.store.get('video', VID)
        self.assertEqual(video['sources']['import']['text'], 'A complete pasted transcript.')

    def test_missing_keys_fail_truthfully(self):
        def fixture_missing(provider, *args, **options):
            if provider == 'antigravity':
                raise providers.ProviderError('setup', 'Fixture has no local Antigravity source.')
            return providers.fetch(provider, *args, **options)
        self.app.fetcher=fixture_missing
        job=self.fetch_job(compare=True)
        with self.assertRaisesRegex(RuntimeError,'API key'):
            self.app.fetch_job(job,threading.Event())
        video=self.app.store.get('video',VID)
        self.assertFalse(video['sources'])
        self.assertEqual([a['provider'] for a in video['attempts']],['supadata','gemini','fetchtranscript','antigravity'])
        self.assertEqual([a['code'] for a in video['attempts']], ['missing_key', 'missing_key', 'missing_key', 'setup'])

    def test_cancel_prevents_provider_call(self):
        job=self.fetch_job()
        canceled=threading.Event();canceled.set()
        with self.assertRaises(InterruptedError):
            self.app.fetch_job(job,canceled)
        self.assertEqual(self.calls,[])

    def test_delete_refuses_while_a_task_is_active_then_removes_the_video_and_its_jobs(self):
        job = self.fetch_job()
        with self.assertRaisesRegex(ValueError, 'Cancel it first'):
            self.app.delete_video(VID)
        self.app.fetch_job(job, threading.Event())
        self.app.store.update('job', job['id'], status='completed')
        other = self.app.add_batch('abcdefghijk')['jobs'][0]
        self.assertEqual(self.app.delete_video(VID), {'deleted': VID})
        self.assertIsNone(self.app.store.get('video', VID))
        self.assertEqual([j['id'] for j in self.app.store.list('job')], [other])
        self.assertIsNotNone(self.app.store.get('video', 'abcdefghijk'))
        with self.assertRaisesRegex(ValueError, 'Video not found'):
            self.app.delete_video(VID)
        with self.assertRaisesRegex(ValueError, 'Video not found'):
            self.app.submit(VID, 'summary')  # a concurrent AI request cannot recreate work after deletion

    def test_removing_canceled_queued_work_does_not_stop_the_worker_or_leave_batch_state(self):
        removed = self.fetch_job()
        self.assertTrue(self.app.cancel(removed['id']))
        self.assertEqual(self.app.remove_activity(removed['id']), {'removed': 1})

        deleted_video = 'abcdefghijk'
        deleted_job = self.app.add_batch(deleted_video)['jobs'][0]
        self.assertTrue(self.app.cancel(deleted_job))
        self.assertEqual(self.app.delete_video(deleted_video), {'deleted': deleted_video})

        survivor = self.app.add_batch('zyxwvutsrqp')['jobs'][0]
        worker = threading.Thread(target=self.app.work, name='fixture-worker')
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.app.store.get('job', survivor)['status'] in ('queued', 'running'):
                time.sleep(0.01)
        finally:
            self.app.stop.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(self.app.store.get('job', survivor)['status'], 'completed')
        self.assertEqual(self.app.gemini_batches, {})
        self.assertIsNone(self.app.store.get('video', deleted_video))

    def test_activity_entries_are_removed_one_or_all_finished_but_never_an_active_task(self):
        done = self.fetch_job()
        self.app.fetch_job(done, threading.Event())
        self.app.store.update('job', done['id'], status='completed')
        failed, queued = [self.app.add_batch(v)['jobs'][0] for v in ('abcdefghijk', 'zyxwvutsrqp')]
        self.app.store.update('job', failed, status='failed')
        with self.assertRaisesRegex(ValueError, 'Cancel it first'):
            self.app.remove_activity(queued)
        self.assertEqual(self.app.remove_activity(failed), {'removed': 1})
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            self.app.remove_activity(failed)
        self.assertEqual(self.app.remove_activity(finished=True), {'removed': 1})
        self.assertEqual([j['id'] for j in self.app.store.list('job')], [queued])  # the queued task stays
        self.assertTrue(self.app.store.get('video', VID)['sources'])  # the saved transcript stays

    def test_batch_position_counts_only_newly_queued_unique_videos(self):
        self.app.fetch_job(self.fetch_job(), threading.Event())
        result = self.app.add_batch(VID+'\nabcdefghijk\nabcdefghijk\nzyxwvutsrqp')
        jobs = [self.app.store.get('job', jid) for jid in result['jobs']]
        self.assertEqual(result['reused'], 1)
        self.assertEqual([j['batch_index'] for j in jobs], [1, 2])
        self.assertEqual([j['batch_total'] for j in jobs], [2, 2])
        self.assertEqual(jobs[0]['batch_id'], jobs[1]['batch_id'])

    def test_shared_batch_cases_match_the_browser_copy(self):
        # static/urls.js counts ready and invalid lines for the Add videos preview; tests/urls.test.js uses the same cases.
        for case in json.loads((Path(__file__).parent / 'video_link_cases.json').read_text(encoding='utf-8'))['batches']:
            with self.subTest(text=case['text']):
                if case['ready'] + case['invalid'] == 0:
                    with self.assertRaises(ValueError):
                        self.app.add_batch(case['text'])
                    continue
                result = self.app.add_batch(case['text'])
                self.assertEqual((len(result['jobs']) + result['reused'], len(result['errors'])), (case['ready'], case['invalid']))
                for job in self.app.store.list('job'):  # let the next case queue the same videos again
                    self.app.store.update('job', job['id'], status='canceled')

    def test_gemini_models_are_listed_for_the_saved_key_and_kept_ten_minutes(self):
        calls = []
        def lister(key):
            calls.append(key)
            return [{'id': 'gemini-x', 'label': 'X', 'input_tokens': 1}]
        self.app.model_lister = lister
        with self.assertRaises(providers.ProviderError) as found:
            self.app.gemini_models()
        self.assertEqual((found.exception.code, str(found.exception)), ('missing_key', 'Save a Gemini key first.'))
        self.assertEqual(calls, [])
        self.app.store.save_settings({'gemini_key': 'key-one'})
        moment = [1000.0]
        self.app.clock = lambda: moment[0]
        self.assertEqual(self.app.gemini_models(), [{'id': 'gemini-x', 'label': 'X', 'input_tokens': 1}])
        moment[0] += 599
        self.app.gemini_models()
        self.assertEqual(calls, ['key-one'])
        moment[0] += 2  # 601 seconds after the list was fetched
        self.app.gemini_models()
        self.assertEqual(calls, ['key-one', 'key-one'])
        self.app.gemini_models()
        self.assertEqual(len(calls), 2)
        self.app.gemini_models(refresh=True)
        self.assertEqual(len(calls), 3)
        self.app.store.save_settings({'gemini_key': 'key-two'})
        self.app.gemini_models()
        self.assertEqual(calls, ['key-one'] * 3 + ['key-two'])
        self.assertNotIn('key-', repr(self.app.model_cache))  # cached by a hash of the key, never the key
        def broken(key):
            raise providers.ProviderError('rate_limit', 'Gemini limit')
        self.app.model_lister = broken
        moment[0] += 700
        for _ in range(2):  # a failure is reported, not cached
            with self.assertRaises(providers.ProviderError):
                self.app.gemini_models()

    def test_last_model_is_the_newest_saved_ai_output_or_answer_of_each_cli(self):
        first = self.app.import_text({'text': 'words', 'title': 'One'})['id']
        second = self.app.import_text({'text': 'more words', 'title': 'Two'})['id']
        video = self.app.store.get('video', first)
        video['outputs']['formatted'] = {'provider': 'codex', 'model': 'gpt-old', 'created': '2026-10-01T10:00:00+00:00'}
        video['conversation'].append({'question': 'q', 'answer': 'a', 'provider': 'codex', 'model': 'gpt-new', 'created': '2026-10-03T10:00:00+00:00'})
        video['outputs']['summary'] = {'provider': 'claude', 'model': 'CLI default', 'created': '2026-10-02T10:00:00+00:00',
                                       'receipts': [{'model': ['claude-opus-5-5']}]}
        self.app.store.put('video', first, video)
        video = self.app.store.get('video', second)
        video['outputs']['summary'] = {'provider': 'codex', 'model': 'gpt-middle', 'created': '2026-10-02T12:00:00+00:00'}
        # Newest of all, but it recorded no model (the CLI said nothing), so it cannot be the last model.
        video['outputs']['formatted'] = {'provider': 'claude', 'model': 'CLI default', 'created': '2026-10-04T10:00:00+00:00', 'receipts': [{'model': []}, {}]}
        self.app.store.put('video', second, video)
        self.assertEqual(self.app.last_models(), {'codex': 'gpt-new', 'claude': 'claude-opus-5-5'})
        info = {'codex': {'installed': True}, 'claude': {'installed': False}, 'other': {'installed': False}}
        with mock.patch.object(ai_cli, 'availability', return_value=info):
            result = self.app.ai_info()
        self.assertEqual({name: entry['last_model'] for name, entry in result.items()}, {'codex': 'gpt-new', 'claude': 'claude-opus-5-5', 'other': None})

    def test_last_model_is_none_before_any_ai_output(self):
        self.app.import_text({'text': 'words'})
        self.assertEqual(self.app.last_models(), {})

    def test_ai_preserves_raw_and_records_source(self):
        vid=self.app.import_text({'text':'raw lowercase text','title':'Synthetic'})['id']
        before=selected(self.app.store.get('video',vid))
        prompts=[]
        def runner(provider,prompt,*args,**kwargs):
            prompts.append(prompt)
            return 'Raw lowercase text.', {'requested_model':'fixture','usage':{}}
        self.app.ai_runner=runner
        jid=self.app.submit(vid,'formatted')
        self.app.ai_job(self.app.store.get('job',jid),threading.Event())
        video=self.app.store.get('video',vid)
        self.assertEqual(selected(video),before)
        self.assertEqual(video['outputs']['formatted']['source_hash'],before['hash'])
        self.assertIn('raw lowercase text',prompts[0])
        self.assertIn('Preserve',prompts[0])

    def test_long_summary_covers_every_part_and_synthesizes(self):
        original='A'*21000+'TAIL EVIDENCE'
        vid=self.app.import_text({'text':original})['id']
        prompts=[]
        def runner(provider,prompt,*args,**kwargs):
            prompts.append(prompt)
            return 'Notes '+str(len(prompts)), {'requested_model':'fixture'}
        self.app.ai_runner=runner
        jid=self.app.submit(vid,'summary')
        self.app.ai_job(self.app.store.get('job',jid),threading.Event())
        self.assertEqual(len(prompts),3)
        self.assertIn('TAIL EVIDENCE',prompts[1])
        self.assertIn('Notes 1',prompts[2])
        self.assertIn('Notes 2',prompts[2])

    def test_question_history_stays_with_source(self):
        vid=self.app.import_text({'text':'There are twelve beds.'})['id']
        prompts=[]
        def runner(provider,prompt,*args,**kwargs):
            prompts.append(prompt)
            return 'Twelve.', {'requested_model':'fixture'}
        self.app.ai_runner=runner
        jid=self.app.submit(vid,'ask',question='How many beds?')
        self.app.ai_job(self.app.store.get('job',jid),threading.Event())
        self.app.store.update('job',jid,status='completed')
        jid=self.app.submit(vid,'ask',question='Repeat the number.')
        self.app.ai_job(self.app.store.get('job',jid),threading.Event())
        self.assertIn('How many beds?',prompts[-1])
        self.assertEqual(len(self.app.store.get('video',vid)['conversation']),2)

    def test_defaults_are_gemini_then_supadata(self):
        self.assertEqual((DEFAULTS['primary'],DEFAULTS['fallback'],DEFAULTS['gemini_model']),('gemini','supadata',gemini.DEFAULT_MODEL))
        self.assertEqual(DEFAULTS['gemini_clip_seconds'], '1200')
        self.assertEqual(gemini.ordered_models(DEFAULTS['gemini_model'], DEFAULTS['gemini_fallback_models']),
                         list(gemini.DEFAULT_MODEL_ORDER))
        with tempfile.TemporaryDirectory() as fresh:
            other=Store(fresh)
            try:
                self.assertEqual((other.config['primary'],other.config['fallback']),('gemini','supadata'))
            finally:
                other.database.close()

    def test_gemini_failure_falls_back_to_supadata_and_gemini_success_does_not(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'supadata'})
        normal=self.app.fetcher
        def gemini_down(provider,*args,**options):
            if provider=='gemini':
                self.calls.append(provider)
                raise providers.ProviderError('rate_limit','Gemini limit')
            return normal(provider,*args,**options)
        self.app.fetcher=gemini_down
        self.app.fetch_job(self.fetch_job(),threading.Event())
        video=self.app.store.get('video',VID)
        self.assertEqual(self.calls,['gemini','supadata'])
        self.assertEqual(video['selected_provider'],'supadata')
        self.assertEqual([a['status'] for a in video['attempts']],['failed','success'])
        self.app.fetcher=normal
        self.calls.clear()
        self.app.add_batch('abcdefghijk')
        job=self.app.store.get('job',self.app.store.list('job')[0]['id'])
        self.app.fetch_job(job,threading.Event())
        other=self.app.store.get('video','abcdefghijk')
        self.assertEqual(self.calls,['gemini'])
        self.assertEqual(other['selected_provider'],'gemini')
        attempt=other['attempts'][0]
        self.assertEqual((attempt['model'],attempt['clips'],attempt['input_tokens'],attempt['output_tokens']),('fixture-model',3,312000,4100))

    def test_failed_refetch_never_replaces_an_existing_complete_transcript(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'none'})
        first = self.fetch_job()
        self.app.fetch_job(first, threading.Event())
        self.app.store.update('job', first['id'], status='completed')
        before = self.app.store.get('video', VID)['sources']['gemini']
        def truncated(*args, **kwargs):
            raise providers.ProviderError('truncated', 'Gemini reached its output-token limit; no partial transcript was saved.')
        self.app.fetcher = truncated
        refetch = self.app.add_batch(VID, mode='gemini', refetch=True)
        job = self.app.store.get('job', refetch['jobs'][0])
        with self.assertRaises(RuntimeError):
            self.app.fetch_job(job, threading.Event())
        after = self.app.store.get('video', VID)['sources']['gemini']
        self.assertEqual(after, before)
        self.assertEqual(self.app.store.get('video', VID)['attempts'][-1]['code'], 'truncated')

    def test_generic_second_clip_400_preserves_the_existing_gemini_source(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'none'})
        first = self.fetch_job()
        self.app.fetch_job(first, threading.Event())
        self.app.store.update('job', first['id'], status='completed')
        before = self.app.store.get('video', VID)['sources']['gemini']
        self.app.store.keys['gemini'] = 'fixture-key'
        requests = []
        def actual(provider, vid, key, language, **options):
            def opener(request, **kwargs):
                requests.append(request)
                if len(requests) == 1:
                    return Response(GEMINI)
                body = json.dumps({'error': {'message': 'Request contains an invalid argument.'}}).encode()
                raise urllib.error.HTTPError(request.full_url, 400, 'Bad Request', {}, io.BytesIO(body))
            return providers.fetch(provider, vid, key, language, opener=opener,
                                   duration_reader=lambda video_id, cancel: None,
                                   sleep=lambda seconds: None, **options)
        self.app.fetcher = actual
        refetch = self.app.add_batch(VID, mode='gemini', refetch=True)
        with self.assertRaises(RuntimeError):
            self.app.fetch_job(self.app.store.get('job', refetch['jobs'][0]), threading.Event())
        video = self.app.store.get('video', VID)
        self.assertEqual(video['sources']['gemini'], before)
        self.assertEqual((video['attempts'][-1]['code'], len(requests)), ('invalid_request', 2))

    def test_unknown_duration_warning_is_saved_in_source_activity_checks_and_exports(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'none'})
        normal = self.app.fetcher
        def warning_result(provider, *args, **options):
            result = normal(provider, *args, **options)
            result.update(end_confirmed=False, end_detected_by='clip_shorter_than_first',
                          completion_warning=gemini.END_NOT_CONFIRMED)
            return result
        self.app.fetcher = warning_result
        job = self.fetch_job()
        self.app.fetch_job(job, threading.Event())
        video = self.app.store.get('video', VID)
        source = video['sources']['gemini']
        attempt = video['attempts'][-1]
        saved_job = self.app.store.get('job', job['id'])
        self.assertEqual(source['completion_warning'], gemini.END_NOT_CONFIRMED)
        self.assertIn(gemini.END_NOT_CONFIRMED, attempt['message'])
        self.assertIn(gemini.END_NOT_CONFIRMED, saved_job['message'])
        self.assertEqual(saved_job['provider_attempts'][0]['completion_warning'], gemini.END_NOT_CONFIRMED)
        raw_export, _ = self.app.export(VID, 'raw')
        json_export, _ = self.app.export(VID, 'json')
        self.assertIn(gemini.END_NOT_CONFIRMED, raw_export)
        self.assertEqual(json.loads(json_export)['sources']['gemini']['completion_warning'],
                         gemini.END_NOT_CONFIRMED)

    def test_gemini_options_reach_the_fetcher_and_progress_updates_the_job(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'none','gemini_model':'gemini-test-2',
                                      'gemini_fallback_models':'gemini-test-3\ngemini-test-4',
                                      'gemini_clip_seconds':'1800'})
        job=self.fetch_job()
        cancel=threading.Event()
        normal=self.app.fetcher
        seen={}
        def fetcher(provider,vid,key,language,**options):
            seen.update(options)
            options['progress']('Gemini · transcribing 10:00-20:00 (part 2)')
            seen['message']=self.app.store.get('job',job['id'])['message']
            cancel.set()
            options['progress']('Gemini · too late')
            seen['after_cancel']=self.app.store.get('job',job['id'])['message']
            return normal(provider,vid,key,language,**options)
        self.app.fetcher=fetcher
        self.app.fetch_job(job,cancel)
        self.assertEqual(seen['model'],'gemini-test-2')
        self.assertEqual((seen['fallback_models'], seen['clip_seconds']), ('gemini-test-3\ngemini-test-4', '1800'))
        self.assertIs(seen['exhausted_models'], self.app.gemini_batches[job['batch_id']]['exhausted'])
        self.assertIs(seen['busy_models'], self.app.gemini_batches[job['batch_id']]['busy'])
        self.assertIs(seen['quota_unknown_models'], self.app.gemini_batches[job['batch_id']]['quota_unknown'])
        self.assertIs(seen['cancel'],cancel)
        self.assertEqual(seen['message'],'Gemini · transcribing 10:00-20:00 (part 2)')
        self.assertEqual(seen['after_cancel'],seen['message'])

    def test_real_dispatch_sends_only_provider_specific_options_and_persists_clip_models(self):
        dispatch = tempfile.TemporaryDirectory()
        self.addCleanup(dispatch.cleanup)
        other = Application(dispatch.name, start_worker=False,
                            antigravity_work_root=Path(dispatch.name) / 'antigravity-runtime',
                            start_readiness_check=False)
        self.addCleanup(other.store.database.close)
        other.store.keys['gemini'] = 'fixture-key'
        common = {**providers.normalize('fetchtranscript', FETCH, VID), 'elapsed_seconds': 0.01}
        gemini_source = {**common, 'provider': 'gemini', 'model': 'gemini-a', 'models': ['gemini-a', 'gemini-b'],
                         'clips': 3, 'requests': 4, 'raw_response': {'clips': [
                             {'model': 'gemini-a'}, {'model': 'gemini-a'}, {'model': 'gemini-b'}]}}
        antigravity_result = {**common, 'provider': 'antigravity', 'model': 'CLI default'}
        with mock.patch.object(gemini, 'fetch', return_value=gemini_source) as gemini_fetch, \
                mock.patch.object(antigravity_source, 'fetch', return_value=antigravity_result) as agy_fetch:
            first = other.add_batch(VID, mode='gemini')
            first_job = other.store.get('job', first['jobs'][0])
            other.fetch_job(first_job, threading.Event())
            other.store.update('job', first_job['id'], status='completed')
            second = other.add_batch(VID, mode='antigravity', refetch=True)
            other.fetch_job(other.store.get('job', second['jobs'][0]), threading.Event())
        gemini_options = gemini_fetch.call_args.kwargs
        self.assertNotIn('work_root', gemini_options)
        self.assertNotIn('antigravity_model', gemini_options)
        self.assertEqual(gemini_options['model'], DEFAULTS['gemini_model'])
        agy_options = agy_fetch.call_args.kwargs
        self.assertNotIn('fallback_models', agy_options)
        self.assertNotIn('clip_seconds', agy_options)
        self.assertEqual(agy_options['work_root'], Path(dispatch.name) / 'antigravity-runtime')
        saved = other.store.get('video', VID)['sources']['gemini']
        self.assertEqual(saved['clip_model_summary'], 'clips 1-2: gemini-a; clip 3: gemini-b')
        saved_job = other.store.get('job', first_job['id'])
        self.assertIn('clips 1-2: gemini-a; clip 3: gemini-b', saved_job['message'])
        self.assertEqual(saved_job['provider_attempts'][0]['clip_models'][2], {'clip': 3, 'model': 'gemini-b'})

    def test_daily_exhaustion_state_carries_across_one_batch_and_not_another(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'none'})
        result = self.app.add_batch('abcdefghijk\nzyxwvutsrqp')
        jobs = [self.app.store.get('job', jid) for jid in result['jobs']]
        normal, states = self.app.fetcher, []
        def fetcher(provider, vid, key, language, **options):
            states.append((vid, options['exhausted_models']))
            if len(states) == 1:
                options['exhausted_models']['gemini-3.8-flash'] = 'fixture-reset'
            elif len(states) == 2:
                self.assertIn('gemini-3.8-flash', options['exhausted_models'])
            result = normal(provider, vid, key, language, **options)
            result['models'] = ['fixture-model']
            return result
        self.app.fetcher = fetcher
        for job in jobs:
            self.app.fetch_job(job, threading.Event())
        self.assertIs(states[0][1], states[1][1])
        third = self.app.add_batch('mnopqrstuvw')
        third_job = self.app.store.get('job', third['jobs'][0])
        self.app.fetch_job(third_job, threading.Event())
        self.assertIsNot(states[1][1], states[2][1])
        self.assertNotIn('gemini-3.8-flash', states[2][1])
        attempt = self.app.store.get('video', 'abcdefghijk')['attempts'][0]
        self.assertEqual(attempt['models'], ['fixture-model'])

    def test_capacity_state_resets_for_each_video_and_failed_provenance_is_saved(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'none'})
        result = self.app.add_batch('abcdefghijk\nzyxwvutsrqp')
        jobs = [self.app.store.get('job', jid) for jid in result['jobs']]
        normal, states = self.app.fetcher, []
        def fetcher(provider, vid, key, language, **options):
            states.append(options['busy_models'])
            if len(states) == 1:
                options['busy_models'].add('gemini-3.8-flash')
                error = providers.ProviderError('capacity', 'All configured Gemini models are temporarily busy.', 503)
                error.requests = 1
                error.availability_failures = [{'model': 'gemini-3.8-flash', 'clip_start': 0,
                                                'message': 'high demand', 'http_status': 503, 'attempts': 1}]
                error.quota_failures = []
                raise error
            return normal(provider, vid, key, language, **options)
        self.app.fetcher = fetcher
        with self.assertRaises(RuntimeError):
            self.app.fetch_job(jobs[0], threading.Event())
        self.app.fetch_job(jobs[1], threading.Event())
        self.assertIs(states[0], states[1])
        self.assertNotIn('gemini-3.8-flash', states[1])
        attempt = self.app.store.get('video', 'abcdefghijk')['attempts'][0]
        self.assertEqual((attempt['code'], attempt['requests']), ('capacity', 1))
        self.assertEqual(attempt['availability_failures'][0]['model'], 'gemini-3.8-flash')
        third = self.app.add_batch('mnopqrstuvw')
        self.app.fetch_job(self.app.store.get('job', third['jobs'][0]), threading.Event())
        self.assertIsNot(states[1], states[2])
        self.assertNotIn('gemini-3.8-flash', states[2])

    def test_unspecified_quota_resets_for_each_video_and_allows_provider_fallback(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'supadata'})
        result = self.app.add_batch('abcdefghijk\nzyxwvutsrqp')
        jobs = [self.app.store.get('job', jid) for jid in result['jobs']]
        normal, states = self.app.fetcher, []
        failure = {'model': 'gemini-3.8-flash', 'reason': 'unspecified_quota', 'clip_start': 0,
                   'clip_end': 1200, 'message': 'quota type unspecified', 'http_status': 429,
                   'attempts': 1, 'request_number': 1}
        def fetcher(provider, vid, key, language, **options):
            if provider == 'gemini':
                states.append(options['quota_unknown_models'])
                if len(states) == 1:
                    self.calls.append(provider)
                    options['quota_unknown_models'].add('gemini-3.8-flash')
                    error = providers.ProviderError('unspecified_quota', 'No reset time is known.', 429)
                    error.requests = 1
                    error.quota_unknown_failures = [failure]
                    raise error
            return normal(provider, vid, key, language, **options)
        self.app.fetcher = fetcher
        self.app.fetch_job(jobs[0], threading.Event())
        self.app.fetch_job(jobs[1], threading.Event())
        self.assertIs(states[0], states[1])
        self.assertNotIn('gemini-3.8-flash', states[1])
        first_video = self.app.store.get('video', 'abcdefghijk')
        self.assertEqual((first_video['selected_provider'], [a['provider'] for a in first_video['attempts']]),
                         ('supadata', ['gemini', 'supadata']))
        self.assertEqual(first_video['attempts'][0]['quota_unknown_failures'], [failure])
        self.assertEqual(self.app.store.get('job', jobs[0]['id'])['provider_attempts'][0]['requests'], 1)

        third = self.app.add_batch('mnopqrstuvw')
        self.app.fetch_job(self.app.store.get('job', third['jobs'][0]), threading.Event())
        self.assertIsNot(states[1], states[2])
        self.assertEqual(states[2], set())

    def test_completed_activity_keeps_unspecified_quota_switch_provenance(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'none'})
        job = self.fetch_job()
        normal = self.app.fetcher
        failure = {'model': 'gemini-a', 'reason': 'unspecified_quota', 'clip_start': 600,
                   'clip_end': 1200, 'next_model': 'gemini-b', 'message': 'quota type unspecified',
                   'http_status': 429, 'attempts': 1, 'request_number': 2}
        def switched(provider, vid, key, language, **options):
            result = normal(provider, vid, key, language, **options)
            result['models'] = ['gemini-a', 'gemini-b']
            result['requests'] = 4
            result['quota_unknown_failures'] = [failure]
            return result
        self.app.fetcher = switched
        self.app.fetch_job(job, threading.Event())
        saved = self.app.store.get('job', job['id'])
        self.assertIn('quota switch: gemini-a unavailable (quota type unspecified) at 10:00', saved['message'])
        self.assertEqual(saved['quota_unknown_failures'], [failure])
        self.assertEqual(saved['provider_attempts'][0]['quota_unknown_failures'][0]['request_number'], 2)

    def test_completed_activity_keeps_capacity_switch_models_and_clip(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'none'})
        job = self.fetch_job()
        normal = self.app.fetcher
        def switched(provider, vid, key, language, **options):
            result = normal(provider, vid, key, language, **options)
            result['models'] = ['gemini-a', 'gemini-b']
            result['availability_failures'] = [{'model': 'gemini-a', 'clip_start': 600,
                                                'next_model': 'gemini-b', 'attempts': 1}]
            return result
        self.app.fetcher = switched
        self.app.fetch_job(job, threading.Event())
        saved = self.app.store.get('job', job['id'])
        self.assertIn('capacity switch: gemini-a busy at 10:00 → gemini-b', saved['message'])
        self.assertEqual(saved['availability_failures'][0]['clip_start'], 600)
        self.assertEqual(saved['provider_attempts'][0]['availability_failures'][0]['next_model'], 'gemini-b')

    def test_comparison_covers_the_services_that_have_keys_and_reports_the_rest(self):
        self.app.store.save_settings({'primary':'gemini','fallback':'supadata'})
        self.app.store.keys.update({'gemini':'key-one','supadata':'key-two'})
        self.app.fetch_job(self.fetch_job(compare=True),threading.Event())
        self.assertEqual(self.calls,['gemini','supadata','antigravity'])
        self.assertEqual(set(self.app.store.get('video',VID)['sources']),{'gemini','supadata','antigravity'})
        normal=self.app.fetcher
        def needs_key(provider,vid,key,language,**options):
            if not key and provider != 'antigravity':
                raise providers.ProviderError('missing_key',f'Add a {providers.LABELS[provider]} API key in Settings.')
            return normal(provider,vid,key,language,**options)
        self.app.fetcher=needs_key
        self.app.store.keys.pop('gemini')
        self.app.add_batch('abcdefghijk',compare=True)
        job=self.app.store.get('job',self.app.store.list('job')[0]['id'])
        self.app.fetch_job(job,threading.Event())
        attempts=self.app.store.get('video','abcdefghijk')['attempts']
        self.assertEqual([(a['provider'],a['status']) for a in attempts],[('supadata','success'),('antigravity','success')])

    def test_gemini_key_and_model_are_saved_validated_and_hidden(self):
        result=self.app.store.save_settings({'gemini_key':'gem-secret','gemini_model':'gemini-3.9-flash',
                                             'gemini_fallback_models':'gemini-3.8-flash\ngemini-3.6-flash',
                                             'gemini_clip_seconds':'900','primary':'gemini','fallback':'none'})
        self.assertNotIn('gem-secret',json.dumps(result))
        self.assertTrue(result['keys_configured']['gemini'])
        self.assertFalse(result['keys_configured']['supadata'])
        self.assertEqual(result['gemini_model'],'gemini-3.9-flash')
        self.assertEqual((result['gemini_fallback_models'], result['gemini_clip_seconds']),
                         ('gemini-3.8-flash\ngemini-3.6-flash', '900'))
        other=Store(self.temp.name)
        try:
            self.assertEqual(other.key('gemini'),'gem-secret')
            self.assertEqual((other.config['primary'],other.config['fallback'],other.config['gemini_model']),('gemini','none','gemini-3.9-flash'))
        finally:
            other.database.close()
        for model in ['','a','x'*81,'has space','semi;colon','model/path']:
            with self.subTest(model=model), self.assertRaises(ValueError):
                self.app.store.save_settings({'gemini_model':model})
        for data in [{'primary':'nope'},{'fallback':'nope'},{'primary':'gemini','fallback':'gemini'},
                     {'gemini_key':'two\nlines'},{'gemini_fallback_models':'bad model'},
                     {'gemini_clip_seconds':'59'},{'gemini_clip_seconds':'3601'},{'gemini_clip_seconds':'1.5'}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.app.store.save_settings(data)
        self.assertEqual(self.app.store.config['gemini_model'],'gemini-3.9-flash')
        self.assertTrue(self.app.store.save_settings({'primary':'supadata','fallback':'gemini'})['keys_configured']['gemini'])
        self.assertFalse(self.app.store.save_settings({'clear_gemini':True})['keys_configured']['gemini'])
        with mock.patch.dict(os.environ,{'GEMINI_API_KEY':'from-environment'}):
            self.assertEqual(self.app.store.key('gemini'),'from-environment')
            self.assertTrue(self.app.store.public_settings()['keys_configured']['gemini'])
            self.app.store.keys['gemini']='saved-wins'
            self.assertEqual(self.app.store.key('gemini'),'saved-wins')

    def test_a_gemini_key_is_removed_from_job_failure_text(self):
        self.app.store.keys['gemini']='gem-secret-key'
        self.assertEqual(self.app.scrub('Gemini said gem-secret-key is wrong'),'Gemini said [API key redacted] is wrong')

    def test_old_settings_gain_clip_and_fallback_defaults_without_losing_the_selected_model(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'settings.json').write_text(json.dumps({
                'settings': {'gemini_model': 'gemini-user-choice', 'primary': 'gemini', 'fallback': 'none'},
                'keys': {}}), encoding='utf-8')
            store = Store(folder)
            try:
                self.assertEqual(store.config['gemini_model'], 'gemini-user-choice')
                self.assertEqual(store.config['gemini_clip_seconds'], '1200')
                self.assertEqual(gemini.ordered_models(store.config['gemini_model'], store.config['gemini_fallback_models'])[0],
                                 'gemini-user-choice')
            finally:
                store.database.close()

    def test_settings_keys_hidden_and_persisted(self):
        result=self.app.store.save_settings({'supadata_key':'test-secret','instructions':'Keep hedging.'})
        self.assertNotIn('test-secret',json.dumps(result))
        self.assertTrue(result['keys_configured']['supadata'])
        other=Store(self.temp.name)
        try:
            self.assertEqual(other.key('supadata'),'test-secret')
            self.assertEqual(other.config['instructions'],'Keep hedging.')
        finally:
            other.database.close()

    def test_keys_saved_tells_a_saved_key_from_an_environment_key(self):
        self.assertEqual(self.app.store.public_settings()['keys_saved'], {'gemini': False, 'supadata': False, 'fetchtranscript': False, 'antigravity': False})
        with mock.patch.dict(os.environ, {'SUPADATA_API_KEY': 'from-environment'}):
            result = self.app.store.public_settings()
            self.assertEqual((result['keys_saved']['supadata'], result['keys_configured']['supadata']), (False, True))
            result = self.app.store.save_settings({'gemini_key': 'gem-secret', 'supadata_key': 'sup-secret'})
        self.assertEqual(result['keys_saved'], {'gemini': True, 'supadata': True, 'fetchtranscript': False, 'antigravity': False})
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(self.app.store.save_settings({'clear_supadata': True})['keys_saved'], {'gemini': True, 'supadata': False, 'fetchtranscript': False, 'antigravity': False})

    def test_restart_marks_unfinished_job_interrupted(self):
        job=self.fetch_job()
        other=Store(self.temp.name)
        try:
            self.assertEqual(other.get('job',job['id'])['status'],'interrupted')
        finally:
            other.database.close()

    def test_exports_are_separated_and_combined(self):
        self.app.import_text({'title':'Alpha','text':'First transcript'})
        self.app.import_text({'title':'Beta','text':'Second transcript'})
        with zipfile.ZipFile(io.BytesIO(self.app.archive())) as result:
            self.assertEqual(len(result.namelist()),3)
            combined=result.read('all-transcripts.md').decode()
            self.assertIn('First transcript',combined)
            self.assertIn('Second transcript',combined)


class AiTests(unittest.TestCase):
    def test_chunks_cover_every_character(self):
        text=('hello\n'*5000)+'last'
        self.assertEqual(''.join(ai_cli.chunks(text)),text)
        self.assertTrue(all(len(c)<=20000 for c in ai_cli.chunks(text)))

    def test_codex_success_and_failure_events(self):
        raw='\n'.join(json.dumps(e) for e in [{'type':'thread.started','thread_id':'own-thread'},
             {'type':'item.completed','item':{'type':'agent_message','text':'answer'}},
             {'type':'turn.completed','usage':{'input_tokens':5}}])
        text,receipt=ai_cli.parse_output('codex',raw)
        self.assertEqual(text,'answer');self.assertEqual(receipt['usage']['input_tokens'],5)
        with self.assertRaises(RuntimeError):
            ai_cli.parse_output('codex',raw+'\n'+json.dumps({'type':'turn.failed','error':'rate limit'}))

    def test_service_keys_are_not_passed_to_the_cli(self):
        captured = {}
        class Stop(Exception):
            pass
        def popen(args, **kwargs):
            captured.update(kwargs['env'])
            raise Stop()
        with tempfile.TemporaryDirectory() as work, mock.patch.dict(os.environ, {name: 'secret' for name in SERVICE_KEYS}), \
                mock.patch.object(ai_cli, 'command', return_value=['cli']), mock.patch.object(ai_cli.subprocess, 'Popen', popen):
            with self.assertRaises(Stop):
                ai_cli.invoke('codex', 'prompt', work)
        self.assertIn('PYTHONIOENCODING', captured)
        for name in SERVICE_KEYS:
            self.assertNotIn(name, captured)

    def test_claude_error_is_not_treated_as_answer(self):
        with self.assertRaisesRegex(RuntimeError,'login'):
            ai_cli.parse_output('claude',json.dumps({'is_error':True,'result':'login required'}))
        text,_=ai_cli.parse_output('claude',json.dumps({'subtype':'success','result':'answer','usage':{}}))
        self.assertEqual(text,'answer')


CODEX_CACHE = {'fetched_at': '2026-10-14T00:00:00Z', 'models': [
    {'slug': 'gpt-6.1-sol', 'display_name': 'GPT-6.1-Sol', 'visibility': 'list', 'default_reasoning_level': 'medium',
     'supported_reasoning_levels': [{'effort': 'low', 'description': 'Fast'}, {'effort': 'medium', 'description': 'Balanced'},
                                    {'effort': 'high', 'description': 'Deep'}, {'effort': 'ultra', 'description': 'Not offered here'}]},
    {'slug': 'gpt-6.1-mini', 'display_name': 'GPT-6.1-Mini', 'visibility': 'list', 'default_reasoning_level': 'low',
     'supported_reasoning_levels': [{'effort': 'low'}]},
    {'slug': 'gpt-no-name', 'visibility': 'list', 'supported_reasoning_levels': 'none'},
    {'slug': 'gpt-hidden', 'display_name': 'Hidden', 'visibility': 'hide', 'supported_reasoning_levels': []},
    {'slug': 'bad slug!', 'display_name': 'Bad', 'visibility': 'list'},
    {'display_name': 'No slug', 'visibility': 'list'}, 'not an entry']}


class CliListTests(unittest.TestCase):
    """ai_cli.availability() and its helpers, read from fixture folders only (never the real home folder)."""
    def home(self, config=None, cache=None, claude=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        home = Path(folder.name)
        for relative, content in ((Path('.codex') / 'config.toml', config), (Path('.codex') / 'models_cache.json', cache),
                                  (Path('.claude') / 'settings.json', claude)):
            if content is not None:
                (home / relative).parent.mkdir(exist_ok=True)
                (home / relative).write_text(content if isinstance(content, str) else json.dumps(content), encoding='utf-8')
        return home

    def availability(self, home, installed=('codex', 'claude', 'antigravity', 'opencode')):
        def executable(provider):
            if provider not in installed:
                raise RuntimeError('not found')
            return ['C:/fixture/' + provider + '.exe']
        with mock.patch.object(ai_cli, 'executable', executable):
            return ai_cli.availability(home)

    def test_codex_defaults_and_models_come_from_its_own_files(self):
        home = self.home('model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n', CODEX_CACHE)
        codex = self.availability(home)['codex']
        self.assertEqual(codex, {
            'installed': True, 'executable': 'C:/fixture/codex.exe', 'label': 'Codex CLI',
            'default_model': 'gpt-6.1-sol', 'default_effort': 'high', 'efforts': ['low', 'medium', 'high', 'xhigh', 'max'],
            'models': [{'id': 'gpt-6.1-sol', 'label': 'GPT-6.1-Sol', 'efforts': ['low', 'medium', 'high'], 'default_effort': 'medium'},
                       {'id': 'gpt-6.1-mini', 'label': 'GPT-6.1-Mini', 'efforts': ['low'], 'default_effort': 'low'},
                       {'id': 'gpt-no-name', 'label': 'gpt-no-name', 'efforts': [], 'default_effort': None}]})

    def test_claude_default_model_comes_from_its_settings_and_aliases_are_fixed(self):
        claude = self.availability(self.home(claude={'model': 'claude-opus-5-5', 'other': 1}))['claude']
        self.assertEqual(claude, {
            'installed': True, 'executable': 'C:/fixture/claude.exe', 'label': 'Claude CLI', 'default_model': 'claude-opus-5-5',
            'default_effort': None, 'efforts': ['low', 'medium', 'high', 'xhigh', 'max'],
            'models': [{'id': 'opus', 'label': 'Opus'}, {'id': 'sonnet', 'label': 'Sonnet'}, {'id': 'haiku', 'label': 'Haiku'}]})

    def test_registry_has_exactly_the_four_clis_and_reports_not_installed(self):
        result = self.availability(self.home(), installed=())
        self.assertEqual(list(result), ['codex', 'claude', 'antigravity', 'opencode'])
        for entry in result.values():
            self.assertFalse(entry['installed'])
            self.assertNotIn('executable', entry)
            self.assertEqual(set(entry), {'installed', 'label', 'default_model', 'default_effort', 'models', 'efforts'})
        self.assertEqual(self.availability(self.home(), installed=('claude',))['claude']['installed'], True)

    def test_missing_or_unreadable_files_give_empty_answers(self):
        for home in (self.home(), self.home('not [valid toml', '{broken', '{broken'), self.home('', {'models': 'none'}, ['list']),
                     self.home('', {'models': {'a': 1}}, {'model': 7}), self.home('', [], {'model': '   '})):
            with self.subTest(home=home.name):
                result = self.availability(home)
                self.assertEqual((result['codex']['default_model'], result['codex']['default_effort'], result['codex']['models']), (None, None, []))
                self.assertEqual((result['claude']['default_model'], result['claude']['default_effort']), (None, None))
                self.assertEqual(len(result['claude']['models']), 3)
                self.assertEqual((result['antigravity']['default_model'], result['antigravity']['efforts'], result['antigravity']['models']), (None, [], []))
                self.assertEqual((result['opencode']['default_model'], result['opencode']['efforts'], result['opencode']['models']),
                                 (ai_cli.opencode_cli.DEFAULT_MODEL, [], []))

    def test_antigravity_names_no_default_model_or_effort_and_is_not_started_for_a_fixture_home(self):
        with mock.patch.object(ai_cli, 'antigravity_models', side_effect=AssertionError('the real CLI must not be started')):
            antigravity = self.availability(self.home())['antigravity']
            self.assertFalse(self.availability(self.home(), installed=('codex',))['antigravity']['installed'])
        self.assertEqual(antigravity, {'installed': True, 'executable': 'C:/fixture/antigravity.exe', 'label': 'Antigravity CLI',
                                       'default_model': None, 'default_effort': None, 'models': [], 'efforts': []})

    def test_codex_home_variable_is_used_when_no_home_is_given(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        (Path(folder.name) / 'config.toml').write_text('model = "from-codex-home"\n', encoding='utf-8')
        (Path(folder.name) / 'models_cache.json').write_text(json.dumps(CODEX_CACHE), encoding='utf-8')
        with mock.patch.dict(os.environ, {'CODEX_HOME': folder.name}):
            info = ai_cli.codex_info()
            self.assertEqual((info['default_model'], [m['id'] for m in info['models']][:2]), ('from-codex-home', ['gpt-6.1-sol', 'gpt-6.1-mini']))
        with mock.patch.dict(os.environ, {'CODEX_HOME': os.path.join(folder.name, 'missing')}):
            self.assertEqual((ai_cli.codex_info()['default_model'], ai_cli.codex_info()['models']), (None, []))

    def test_every_effort_offered_is_one_that_settings_accept(self):
        with tempfile.TemporaryDirectory() as data:
            store = Store(data)
            try:
                for effort in ai_cli.EFFORTS:
                    store.save_settings({'ai_effort': effort})
                for model in ai_cli.codex_models(self.home('', CODEX_CACHE)):
                    for effort in model['efforts']:
                        store.save_settings({'ai_effort': effort, 'ai_model': model['id']})
            finally:
                store.database.close()


AGY_USAGE = {'input_tokens': 11, 'output_tokens': 5, 'thinking_tokens': 2, 'cache_read_tokens': 0, 'total_tokens': 16}
AGY_MODEL = 'gemini-3.8-flash-high'


def agy_event(name, **fields):
    """One line of the CLI's stream-json output, as the CLI prints it."""
    return json.dumps({'event': name, **fields})


def agy_result(response='The answer.\n', status='SUCCESS', **more):
    return agy_event('result', result={'conversation_id': 'c-1', 'status': status, 'response': response, 'duration_seconds': 1.5,
                                       'num_turns': 1, 'usage': AGY_USAGE, **more})


AGY_INIT = agy_event('init', conversation_id='c-1', init={'cwd': 'C:/work', 'tools': ['view_file'], 'permission_mode': 'request-review', 'model': AGY_MODEL})
AGY_STEP = agy_event('step_update', step_update={'conversation_id': 'c-1', 'step_index': 1, 'state': 'DONE', 'step_type': 'agent_response'})
AGY_PROMPT = 'Say "hi" and \'bye\'\nline two\r\n\ttab, backslash \\ and \\n, brace {"a": [1]}, caf\u00e9 \u2014 \u65e5\u672c\u8a9e \U0001f600 next\x85line\u2028end'
# Stands in for the Antigravity CLI (agy): `agy models` prints a model list, and any other call reads stdin as the real CLI does
# in stream-json mode and acts out one outcome, named by its first argument. What it was given goes to the file named by
# FAKE_AGY_LOG, never to its working folder, which must stay empty.
FAKE_AGY = r'''import json, os, sys, time
mode, args = sys.argv[1], sys.argv[2:]
data = sys.stdin.buffer.read()
KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_GEMINI_BASE_URL", "GEMINI_BASE_URL", "AGY_ADC_AUTH", "AGY_LLM_GATEWAY_URL", "SUPADATA_API_KEY", "FETCHTRANSCRIPT_API_KEY")
with open(os.environ["FAKE_AGY_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"mode": mode, "args": args, "stdin": data.decode("utf-8"), "cwd": os.getcwd(), "listing": sorted(os.listdir(".")),
                          "leaked": [key for key in KEYS if key in os.environ]}) + "\n")
def say(text):
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
def emit(event):  # raw UTF-8, like a CLI that does not escape what it prints
    say(json.dumps(event, ensure_ascii=False))
if args[:1] == ["models"]:
    if mode == "models-fail":
        sys.stderr.write("Authentication required\n")
        sys.exit(1)
    if mode == "models-broken":
        sys.stderr.write("something broke\n")
        sys.exit(1)
    lines = {"models-junk": ["not a model line", "bad id!\tBad", "a-model\tLabel A", "a-model\tDuplicate", "b-model", "c-model\t", "", "d-model\tLabel D\r"]}
    for line in lines.get(mode, ["m-one\tModel One", "m-two\tModel Two"]):
        say(line)
    sys.exit(0)
content = ""
for line in data.decode("utf-8").split("\n"):
    try:
        found = json.loads(line)
    except ValueError:
        continue
    if isinstance(found, dict) and found.get("event") == "user":
        content = found["message"]["content"]
        break
model = args[args.index("--model") + 1] if "--model" in args else ""
usage = {"input_tokens": 11, "output_tokens": 5, "thinking_tokens": 2, "cache_read_tokens": 0, "total_tokens": 16}
def result(response="", status="SUCCESS", **more):
    return {"event": "result", "result": {"conversation_id": "c-1", "status": status, "response": response, "duration_seconds": 1.5,
                                          "num_turns": 1, "usage": usage, **more}}
def start():
    init = {"cwd": os.getcwd(), "tools": ["view_file"], "permission_mode": "request-review"}
    if model:
        init["model"] = model
    emit({"event": "init", "conversation_id": "c-1", "init": init})
    emit({"event": "step_update", "step_update": {"conversation_id": "c-1", "step_index": 1, "state": "DONE", "step_type": "agent_response"}})
DENIED = [{"action": "run_command", "display_name": "RunCommand"}, {"action": "read_file", "display_name": "ViewFile"}]
if mode == "ok":
    start()
    emit(result("got " + content + "\n"))
elif mode == "junk":
    for text in ("Loading...", "", "{not json", "[1, 2]", json.dumps({"no": "event"}), json.dumps({"event": 7})):
        say(text)
    start()
    emit(result("  Junk is skipped.\n"))
    say("bye")
elif mode == "denied":
    start()
    sys.stderr.write("jetski: no output produced - a tool required a permission that headless mode cannot prompt for\n")
    emit(result("", denied_actions=DENIED))
elif mode == "denied_answer":
    start()
    emit(result("Partial answer.\n", denied_actions=[{"action": "read_url", "display_name": "ReadUrlContent"}]))
elif mode == "empty":
    start()
    emit(result("  \n"))
elif mode == "error_zero":
    start()
    emit(result("", "ERROR", error="model overloaded, try later"))
elif mode == "error_exit":
    emit(result("", "ERROR", error='invalid model selection (--model "x" --effort ""): model x is not recognized\nAvailable models:\n  Gemini 3.8 Flash (High)'))
    sys.exit(1)
elif mode == "canceled":
    start()
    emit(result("", "CANCELED"))
elif mode == "signin_stderr":
    sys.stderr.write("\x1b[31mAuthentication required.\x1b[0m Opening https://accounts.google.com/o/oauth2/v2/auth?client_id=x\n")
    sys.exit(1)
elif mode == "signin_result":
    emit(result("", "ERROR", error="Authentication expired. Please log back in."))
    sys.exit(1)
elif mode == "signin_zero":
    emit(result("", "ERROR", error="authentication failed or timed out"))
elif mode == "answer_about_signin":
    start()
    emit(result("To fix Authentication required errors, sign in again.\n"))
elif mode == "silent":
    pass
elif mode == "crash":
    sys.stderr.write("boom\n")
    sys.exit(3)
elif mode == "crash_quiet":
    sys.exit(4)
elif mode == "litter":
    start()
    open("scratch.txt", "w").write("x")
    emit(result("done\n"))
elif mode == "litter_many":
    start()
    for number in range(1, 8):
        open("f%d.txt" % number, "w").write("x")
    emit(result("done\n"))
elif mode == "litter_long":
    start()
    open("n" * 80 + ".txt", "w").write("x")
    emit(result("done\n"))
elif mode == "hang":
    time.sleep(60)
else:
    sys.exit(9)
'''


class AntigravityCliTests(unittest.TestCase):
    """Antigravity CLI as the third local AI CLI: fixture folders and a fake agy program (never the real one)."""
    def setUp(self):
        ai_cli.ANTIGRAVITY_LIST.clear()
        self.addCleanup(ai_cli.ANTIGRAVITY_LIST.clear)
        self.log = self.folder() / 'fake_agy.log'
        patcher = mock.patch.dict(os.environ, {'FAKE_AGY_LOG': str(self.log)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def folder(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def fake(self, mode):
        script = self.folder() / 'fake_agy.py'
        script.write_text(FAKE_AGY, encoding='utf-8')
        return mock.patch.object(ai_cli, 'executable', lambda provider: [sys.executable, str(script), mode])

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()] if self.log.exists() else []

    def test_executable_is_agy_on_the_path_else_the_installer_folder(self):
        root = self.folder()
        on_path, installed = root / 'path' / 'agy.exe', root / 'local' / 'agy' / 'bin' / 'agy.exe'
        for file in (on_path, installed):
            file.parent.mkdir(parents=True)
            file.write_text('', encoding='utf-8')
        asked, found = [], [str(on_path)]
        def which(name, *args, **kwargs):
            asked.append(name)
            return found[0]
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(root / 'local')}), mock.patch.object(ai_cli.shutil, 'which', which):
            self.assertEqual(ai_cli.executable('antigravity'), [str(on_path.resolve())])
            self.assertEqual(asked, ['agy'])
            found[0] = None  # not on the path (a server started before the installer changed it): the installer's folder
            self.assertEqual(ai_cli.executable('antigravity'), [str(installed.resolve())])
            found[0] = str(root / 'gone' / 'agy.exe')  # a path entry that no longer exists
            self.assertEqual(ai_cli.executable('antigravity'), [str(installed.resolve())])
            installed.unlink()
            with self.assertRaisesRegex(RuntimeError, 'Antigravity CLI was not found'):
                ai_cli.executable('antigravity')
        with mock.patch.dict(os.environ), mock.patch.object(ai_cli.shutil, 'which', lambda *args, **kwargs: None):
            os.environ.pop('LOCALAPPDATA', None)
            with self.assertRaisesRegex(RuntimeError, 'Antigravity CLI was not found'):
                ai_cli.executable('antigravity')
        with self.assertRaisesRegex(ValueError, 'Unknown AI CLI'):
            ai_cli.executable('gemini')

    def test_registry_names_antigravity_and_no_longer_gemini(self):
        self.assertEqual(list(ai_cli.CLIS), ['codex', 'claude', 'antigravity', 'opencode'])
        self.assertEqual(ai_cli.CLIS['antigravity']['label'], 'Antigravity CLI')
        self.assertEqual(ai_cli.CLIS['opencode']['label'], 'OpenCode CLI')
        self.assertNotIn('gemini', ai_cli.PACKAGES)
        self.assertFalse(hasattr(ai_cli, 'GEMINI_SETTINGS'))
        root = Path(ai_cli.__file__).resolve().parent
        self.assertFalse((root / 'gemini_cli_settings.json').exists())



    def test_command_line(self):
        with mock.patch.object(ai_cli, 'executable', side_effect=lambda provider: ['agy']):
            base = ['agy', '--input-format', 'stream-json', '--output-format', 'stream-json', '--disable-slash-commands']
            self.assertEqual(ai_cli.command('antigravity'), base)
            self.assertEqual(ai_cli.command('antigravity', 'claude-sonnet-5-5-medium'), base + ['--model', 'claude-sonnet-5-5-medium'])
            args = ai_cli.command('antigravity', AGY_MODEL, 'high')  # the effort is part of the model id: there is no effort flag
            self.assertEqual(args, base + ['--model', AGY_MODEL])
        for flag in ('-p', '--print', '--prompt', '--effort', '--mode', '--sandbox', '--dangerously-skip-permissions'):
            self.assertNotIn(flag, args)

    def test_the_prompt_is_one_ascii_stream_json_line(self):
        data = ai_cli.antigravity_input(AGY_PROMPT)
        data.decode('ascii')
        self.assertEqual((data.count(b'\n'), data.endswith(b'\n')), (1, True))
        self.assertEqual(json.loads(data), {'event': 'user', 'message': {'content': AGY_PROMPT}})
        self.assertEqual(json.loads(ai_cli.antigravity_input('')), {'event': 'user', 'message': {'content': ''}})

    def test_parse_output_reads_the_result_event(self):
        lines = [AGY_INIT, AGY_STEP, agy_result()]
        texts = {'plain': '\n'.join(lines) + '\n',
                 'junk lines around': 'Loading...\n{not json\n[1, 2]\n{"no": "event"}\n{"event": 7}\n\n' + '\n'.join(lines) + '\nbye\n',
                 'windows line ends': '\r\n'.join(lines) + '\r\n',
                 'no final newline': '\n'.join(lines)}
        for label, text in texts.items():
            with self.subTest(label):
                answer, receipt = ai_cli.parse_output('antigravity', text)
                self.assertEqual(answer, 'The answer.')
                self.assertEqual(receipt, {'conversation_id': 'c-1', 'usage': AGY_USAGE, 'model': [AGY_MODEL]})  # and no denied_actions
        result = {'event': 'result', 'result': {'conversation_id': 'c-2', 'status': 'SUCCESS', 'response': 'Caf\u00e9 \u65e5\u672c\u8a9e \U0001f600\n'}}
        for ensure_ascii in (True, False):  # escaped or raw: the CLI's choice
            with self.subTest(ensure_ascii=ensure_ascii):
                answer, receipt = ai_cli.parse_output('antigravity', json.dumps(result, ensure_ascii=ensure_ascii) + '\n')
                self.assertEqual((answer, receipt), ('Caf\u00e9 \u65e5\u672c\u8a9e \U0001f600', {'conversation_id': 'c-2', 'usage': {}, 'model': []}))
        self.assertEqual(ai_cli.parse_output('antigravity', '\n'.join([AGY_INIT, agy_result('First.'), agy_result('Last.')]))[0], 'Last.')
        self.assertEqual(ai_cli.parse_output('antigravity', agy_event('init', init='odd') + '\n' + agy_result())[1]['model'], [])

    def test_a_line_separator_inside_the_answer_does_not_cut_the_event(self):
        raw = json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'a\x85b\u2028c\u2029d'}}, ensure_ascii=False)
        self.assertIn('\u2028', raw)
        self.assertEqual(ai_cli.parse_output('antigravity', AGY_INIT + '\n' + raw + '\n')[0], 'a\x85b\u2028c\u2029d')

    def test_parse_output_reports_errors_and_unreadable_output(self):
        cases = [('an error status', agy_result('', 'ERROR', error='model overloaded'), 'model overloaded'),
                 ('an error on a success status', agy_result('Text', error='the turn failed'), 'the turn failed'),
                 ('an error status with no text', agy_result('', 'ERROR'), 'Antigravity ended with the status ERROR.'),
                 ('a canceled run', agy_result('', 'CANCELED'), 'Antigravity ended with the status CANCELED.'),
                 ('colored text', agy_result('', 'ERROR', error='\x1b[31mred\x1b[0m failure'), 'red failure'),
                 ('a long text is cut', agy_result('', 'ERROR', error='x' * 3000), 'x' * 1200)]
        for label, text, expected in cases:
            with self.subTest(label), self.assertRaises(RuntimeError) as caught:
                ai_cli.parse_output('antigravity', AGY_INIT + '\n' + text + '\n')
            self.assertEqual(str(caught.exception), expected)
        for error in ('Authentication required', 'authentication failed or timed out', 'Authentication expired. Please log back in.',
                      'opening https://accounts.google.com/o/oauth2/v2/auth to sign in'):
            with self.subTest(error), self.assertRaises(RuntimeError) as caught:
                ai_cli.parse_output('antigravity', agy_result('', 'ERROR', error=error))
            self.assertEqual(str(caught.exception), ai_cli.ANTIGRAVITY_SIGN_IN)
        for text in ('', 'plain words', '[1, 2]', AGY_INIT, agy_event('result', result='odd'), agy_event('step_update', result={'status': 'SUCCESS'})):
            with self.subTest(text[:30]), self.assertRaisesRegex(RuntimeError, 'Antigravity returned an unreadable response'):
                ai_cli.parse_output('antigravity', text)

    def test_the_answers_own_words_are_never_taken_for_a_sign_in_request(self):
        answer, _ = ai_cli.parse_output('antigravity', agy_result('To fix "Authentication required" errors, log back in at accounts.google.com.'))
        self.assertIn('Authentication required', answer)

    def test_denied_actions_are_named_when_they_leave_no_answer_and_kept_when_they_do_not(self):
        denied = [{'action': 'run_command', 'display_name': 'RunCommand'}, {'action': 'read_file', 'display_name': 'ViewFile'}]
        with self.assertRaises(RuntimeError) as caught:
            ai_cli.parse_output('antigravity', AGY_INIT + '\n' + agy_result('  \n', denied_actions=denied))
        self.assertEqual(str(caught.exception), 'Antigravity CLI gave no answer because it refused these actions: '
                         'RunCommand (run_command), ViewFile (read_file). It is meant to run without tools.')
        answer, receipt = ai_cli.parse_output('antigravity', AGY_INIT + '\n' + agy_result('Partial.\n', denied_actions=denied))
        self.assertEqual((answer, receipt['denied_actions']), ('Partial.', denied))
        answer, receipt = ai_cli.parse_output('antigravity', agy_result('Odd.\n', denied_actions=['text', 7, {'action': 'read_url', 'display_name': 'ReadUrlContent'}]))
        self.assertEqual(receipt['denied_actions'], [{'action': 'read_url', 'display_name': 'ReadUrlContent'}])
        for odd in ('text', {}, None, []):  # not a list of actions, or none: an empty answer is then just empty
            answer, receipt = ai_cli.parse_output('antigravity', agy_result('', denied_actions=odd))
            self.assertEqual((answer, 'denied_actions' in receipt), ('', False))

    def test_failures_become_a_sign_in_request_the_clis_own_error_or_its_text(self):
        error = agy_result('', 'ERROR', error='invalid model selection (--model "x"): model x is not recognized')
        signed_out = agy_result('', 'ERROR', error='Authentication expired. Please log back in.')
        cases = [('Authentication required on stderr', 1, '', 'Authentication required\n', ai_cli.ANTIGRAVITY_SIGN_IN),
                 ('authentication failed on stderr', 1, '', 'warning\nauthentication failed or timed out\n', ai_cli.ANTIGRAVITY_SIGN_IN),
                 ('a Google sign-in address on stderr', 1, '', 'Open https://accounts.google.com/o/oauth2/v2/auth?x=1\n', ai_cli.ANTIGRAVITY_SIGN_IN),
                 ('colored sign-in text', 1, '', '\x1b[31mAuthentication required.\x1b[0m\n', ai_cli.ANTIGRAVITY_SIGN_IN),
                 ('a sign-in error in the result event', 1, signed_out, '', ai_cli.ANTIGRAVITY_SIGN_IN),
                 ('the error of the result event', 1, AGY_INIT + '\n' + error, 'noise\n', 'invalid model selection (--model "x"): model x is not recognized'),
                 ('the error wins over stderr text', 2, error, 'something else\n', 'invalid model selection (--model "x"): model x is not recognized'),
                 ('an answer that mentions signing in is not a request to sign in', 3, agy_result('Fix "Authentication required" errors.'), 'AGY_ERROR: model failed\n',
                  'Antigravity exited 3: AGY_ERROR: model failed'),
                 ('plain text', 1, '', 'something broke\n', 'Antigravity exited 1: something broke'),
                 ('colored plain text', 1, '', '\x1b[31mSomething \x1b[1mbad\x1b[0m happened\x1b[0m\n', 'Antigravity exited 1: Something bad happened'),
                 ('plain text on stdout only', 2, 'only on stdout\n', '', 'Antigravity exited 2: only on stdout'),
                 ('no output at all', 1, '', '', 'Antigravity exited 1: No output.'),
                 ('a very long text keeps its end', 3, '', 'a' * 3000 + 'END', 'Antigravity exited 3: ' + 'a' * 1197 + 'END')]
        for label, code, stdout, stderr, expected in cases:
            with self.subTest(label):
                self.assertEqual(ai_cli.antigravity_failure(code, stdout, stderr), expected)

    def test_models_come_from_agy_models_and_are_kept_for_ten_minutes(self):
        now = [1000.0]
        clock = lambda: now[0]
        with self.fake('models-ok'):
            first = ai_cli.antigravity_models(clock)
            self.assertEqual(first, [{'id': 'm-one', 'label': 'Model One'}, {'id': 'm-two', 'label': 'Model Two'}])
            first.append({'id': 'changed'})  # the caller cannot change what is kept
            first[0]['label'] = 'changed'
            self.assertEqual(len(self.calls()), 1)
            self.assertEqual((self.calls()[0]['args'], self.calls()[0]['stdin']), (['models'], ''))
            now[0] += 599
            self.assertEqual(ai_cli.antigravity_models(clock), [{'id': 'm-one', 'label': 'Model One'}, {'id': 'm-two', 'label': 'Model Two'}])
            self.assertEqual(len(self.calls()), 1)
            now[0] += 2  # 601 seconds after the run
            ai_cli.antigravity_models(clock)
            self.assertEqual(len(self.calls()), 2)
        with self.fake('models-ok'), mock.patch.object(ai_cli, 'executable', lambda provider: [sys.executable, 'other-path']):
            self.assertEqual(ai_cli.antigravity_models(clock), [])  # another executable is another list: this one cannot run
        self.assertEqual(len(self.calls()), 2)

    def test_the_model_list_skips_unusable_lines_and_a_failed_run_gives_none_and_is_kept_too(self):
        with self.fake('models-junk'):
            self.assertEqual(ai_cli.antigravity_models(), [{'id': 'a-model', 'label': 'Label A'}, {'id': 'b-model', 'label': 'b-model'},
                                                           {'id': 'c-model', 'label': 'c-model'}, {'id': 'd-model', 'label': 'Label D'}])
        ai_cli.ANTIGRAVITY_LIST.clear()
        before = len(self.calls())
        with self.fake('models-fail'):
            self.assertEqual(ai_cli.antigravity_models(), [])
            self.assertEqual(ai_cli.antigravity_models(), [])  # a signed-out CLI is not started again at once
        self.assertEqual(len(self.calls()), before + 1)

    def test_without_run_the_cli_is_never_started_and_the_last_list_is_given_at_any_age(self):
        now = [1000.0]
        clock = lambda: now[0]
        with self.fake('models-ok'):
            self.assertEqual(ai_cli.antigravity_model_list(clock, run=False), {'models': [], 'error': ''})  # before any run
            self.assertEqual(self.calls(), [])
            ai_cli.antigravity_models(clock)
            now[0] += 3600
            with mock.patch.object(ai_cli.subprocess, 'run', side_effect=AssertionError('started')):
                self.assertEqual(ai_cli.antigravity_models(clock, run=False), [{'id': 'm-one', 'label': 'Model One'}, {'id': 'm-two', 'label': 'Model Two'}])
                self.assertEqual(len(ai_cli.antigravity_info()['models']), 2)  # what a page load gets
        self.assertEqual(len(self.calls()), 1)

    def test_a_model_list_run_that_asks_for_a_sign_in_says_so(self):
        with self.fake('models-fail'):
            self.assertEqual(ai_cli.antigravity_model_list(), {'models': [], 'error': ai_cli.ANTIGRAVITY_SIGN_IN})
            self.assertEqual(ai_cli.antigravity_model_list(run=False), {'models': [], 'error': ai_cli.ANTIGRAVITY_SIGN_IN})  # kept
        ai_cli.ANTIGRAVITY_LIST.clear()
        with self.fake('models-broken'):
            self.assertEqual(ai_cli.antigravity_model_list(), {'models': [], 'error': ''})
        # A signed-out CLI waits 60 seconds for a code, so the 20-second limit stops it; what it printed by then still counts.
        stopped = subprocess.TimeoutExpired('agy', 20, output=b'', stderr=b'Authentication required. Please visit the URL to log in:\nhttps://accounts.google.com/o/oauth2/auth\n')
        for failure, expected in ((stopped, ai_cli.ANTIGRAVITY_SIGN_IN), (subprocess.TimeoutExpired('agy', 20), '')):
            ai_cli.ANTIGRAVITY_LIST.clear()
            with self.subTest(stderr=failure.stderr), mock.patch.object(ai_cli, 'executable', lambda provider: ['agy']), \
                    mock.patch.object(ai_cli.subprocess, 'run', side_effect=failure):
                self.assertEqual(ai_cli.antigravity_model_list(), {'models': [], 'error': expected})

    def test_the_model_list_is_run_without_the_service_keys_and_login_switches(self):
        switches = {name: 'x' for name in (*SERVICE_KEYS, *ai_cli.ANTIGRAVITY_ENV)}
        with self.fake('models-ok'), mock.patch.dict(os.environ, switches):
            ai_cli.antigravity_models()
        self.assertEqual([call['leaked'] for call in self.calls()], [[]])

    def test_the_model_list_is_empty_when_the_cli_is_missing_slow_or_broken(self):
        with mock.patch.object(ai_cli, 'executable', side_effect=RuntimeError('not found')), \
                mock.patch.object(ai_cli.subprocess, 'run', side_effect=AssertionError('started')):
            self.assertEqual(ai_cli.antigravity_models(), [])
        for failure in (subprocess.TimeoutExpired('agy', 20), OSError('cannot start')):
            ai_cli.ANTIGRAVITY_LIST.clear()
            with self.subTest(type(failure).__name__), mock.patch.object(ai_cli, 'executable', lambda provider: ['agy']), \
                    mock.patch.object(ai_cli.subprocess, 'run', side_effect=failure):
                self.assertEqual(ai_cli.antigravity_models(), [])

    def test_info_has_no_default_model_and_no_efforts_and_never_starts_the_cli_for_a_fixture_home(self):
        models = [{'id': 'm-one', 'label': 'Model One'}]
        with mock.patch.object(ai_cli, 'antigravity_models', return_value=models) as listed:
            self.assertEqual(ai_cli.antigravity_info(), {'default_model': None, 'default_effort': None, 'models': models, 'efforts': []})
        listed.assert_called_once_with(run=False)  # the last list only: a page load never starts the CLI
        with mock.patch.object(ai_cli, 'antigravity_models', side_effect=AssertionError('started')):
            self.assertEqual(ai_cli.antigravity_info(self.folder())['models'], [])

    def test_it_runs_in_an_empty_folder_of_its_own_and_refuses_one_with_files(self):
        work, seen = self.folder(), []
        class Stop(Exception):
            pass
        def popen(args, **kwargs):
            seen.append((Path(kwargs['cwd']), list(Path(kwargs['cwd']).iterdir())))
            raise Stop()
        with mock.patch.object(ai_cli, 'command', return_value=['cli']), mock.patch.object(ai_cli.subprocess, 'Popen', popen):
            for provider in ('antigravity', 'codex'):
                with self.assertRaises(Stop):
                    ai_cli.invoke(provider, 'Read @notes.txt', work)
            self.assertEqual(seen, [(work / 'antigravity', []), (work, [work / 'antigravity'])])  # Codex and Claude keep the shared folder
            (work / 'antigravity' / 'notes.txt').write_text('private', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'must be empty'):
                ai_cli.invoke('antigravity', 'Read @notes.txt', work)
        self.assertEqual(len(seen), 2)

    def test_a_file_the_cli_leaves_behind_fails_the_call_and_names_it(self):
        work = self.folder() / 'ai-work'  # does not exist yet: the call creates it
        with self.fake('litter'), self.assertRaises(RuntimeError) as caught:
            ai_cli.invoke('antigravity', 'prompt', work)
        self.assertEqual(str(caught.exception), f"Antigravity CLI left scratch.txt in {work / 'antigravity'}, which must stay empty. "
                                                "Find out why, and remove it before the next call.")
        with self.fake('ok'), self.assertRaisesRegex(RuntimeError, 'must be empty'):  # the next call refuses to start until it is removed
            ai_cli.invoke('antigravity', 'prompt', work)
        self.assertEqual([call['mode'] for call in self.calls()], ['litter'])
        (work / 'antigravity' / 'scratch.txt').unlink()
        with self.fake('ok'):
            self.assertEqual(ai_cli.invoke('antigravity', 'prompt', work)[0], 'got prompt')
        with self.fake('litter_many'), self.assertRaises(RuntimeError) as caught:
            ai_cli.invoke('antigravity', 'prompt', self.folder())
        self.assertIn('left f1.txt, f2.txt, f3.txt, f4.txt, f5.txt and more in ', str(caught.exception))
        with self.fake('litter_long'), self.assertRaises(RuntimeError) as caught:
            ai_cli.invoke('antigravity', 'prompt', self.folder())
        self.assertIn('left ' + 'n' * 60 + ' in ', str(caught.exception))

    def test_environment_is_cleaned_before_the_cli_starts(self):
        captured = {}
        class Stop(Exception):
            pass
        def popen(args, **kwargs):
            captured.clear()
            captured.update(kwargs['env'])
            raise Stop()
        switches = ai_cli.ANTIGRAVITY_ENV
        with mock.patch.dict(os.environ, {name: 'x' for name in (*SERVICE_KEYS, *switches)}), \
                mock.patch.object(ai_cli, 'command', return_value=['cli']), mock.patch.object(ai_cli.subprocess, 'Popen', popen):
            with self.assertRaises(Stop):
                ai_cli.invoke('antigravity', 'prompt', self.folder())
            for name in (*SERVICE_KEYS, *switches):
                self.assertNotIn(name, captured)
            self.assertEqual(captured['PYTHONIOENCODING'], 'utf-8')
            self.assertEqual(captured['PATH'], os.environ['PATH'])  # the rest of the environment is left alone
            with self.assertRaises(Stop):
                ai_cli.invoke('codex', 'prompt', self.folder())
            self.assertIn('GOOGLE_API_KEY', captured)  # only the Antigravity CLI is kept from the Google switches
        self.assertEqual(set(ai_cli.ANTIGRAVITY_ENV), {'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_GENAI_USE_VERTEXAI', 'GOOGLE_CLOUD_PROJECT',
                                                       'GOOGLE_CLOUD_LOCATION', 'GOOGLE_GEMINI_BASE_URL', 'GEMINI_BASE_URL', 'AGY_ADC_AUTH',
                                                       'AGY_LLM_GATEWAY_URL'})

    def test_invoke_sends_the_message_on_stdin_and_returns_the_answer_and_receipt(self):
        work = self.folder()
        switches = {name: 'x' for name in (*SERVICE_KEYS, *ai_cli.ANTIGRAVITY_ENV)}
        with self.fake('ok'), mock.patch.dict(os.environ, switches):
            text, receipt = ai_cli.invoke('antigravity', AGY_PROMPT, work, effort='high')
        self.assertEqual(text, 'got ' + AGY_PROMPT)
        self.assertEqual({key: receipt[key] for key in ('provider', 'requested_model', 'conversation_id', 'usage', 'model')},
                         {'provider': 'antigravity', 'requested_model': 'CLI default', 'conversation_id': 'c-1', 'usage': AGY_USAGE, 'model': []})
        self.assertIsInstance(receipt['elapsed_seconds'], float)
        self.assertNotIn('denied_actions', receipt)
        call, = self.calls()
        self.assertEqual(call['args'], ['--input-format', 'stream-json', '--output-format', 'stream-json', '--disable-slash-commands'])
        self.assertEqual(call['stdin'], ai_cli.antigravity_input(AGY_PROMPT).decode('ascii'))
        self.assertEqual((Path(call['cwd']).resolve(), call['listing'], call['leaked']), ((work / 'antigravity').resolve(), [], []))
        with self.fake('ok'):
            text, receipt = ai_cli.invoke('antigravity', 'q', work, model='claude-sonnet-5-5-medium')
        self.assertEqual((text, receipt['requested_model'], receipt['model']), ('got q', 'claude-sonnet-5-5-medium', ['claude-sonnet-5-5-medium']))
        self.assertEqual(self.calls()[-1]['args'][-2:], ['--model', 'claude-sonnet-5-5-medium'])

    def test_a_long_prompt_arrives_whole(self):
        prompt = '\u00e9' * 60000 + '\nend'
        with self.fake('ok'):
            text, _ = ai_cli.invoke('antigravity', prompt, self.folder())
        self.assertEqual(text, 'got ' + prompt)

    def test_invoke_failures_have_plain_messages(self):
        refused = ('Antigravity CLI gave no answer because it refused these actions: RunCommand (run_command), ViewFile (read_file). '
                   'It is meant to run without tools.')
        cases = [('signin_stderr', ai_cli.ANTIGRAVITY_SIGN_IN), ('signin_result', ai_cli.ANTIGRAVITY_SIGN_IN),
                 ('signin_zero', ai_cli.ANTIGRAVITY_SIGN_IN),
                 ('error_exit', 'invalid model selection (--model "x" --effort ""): model x is not recognized\nAvailable models:\n  Gemini 3.8 Flash (High)'),
                 ('error_zero', 'model overloaded, try later'), ('canceled', 'Antigravity ended with the status CANCELED.'),
                 ('denied', refused), ('silent', 'Antigravity returned an unreadable response.'),
                 ('crash', 'Antigravity exited 3: boom'), ('crash_quiet', 'Antigravity exited 4: No output.'),
                 ('empty', 'The AI returned no final answer.'), ('unknown', 'Antigravity exited 9: No output.')]
        for mode, expected in cases:
            with self.subTest(mode):
                with self.fake(mode), self.assertRaises(RuntimeError) as caught:
                    ai_cli.invoke('antigravity', 'prompt', self.folder())
                self.assertEqual(str(caught.exception), expected)

    def test_an_answer_that_mentions_signing_in_is_returned(self):
        with self.fake('answer_about_signin'):
            self.assertEqual(ai_cli.invoke('antigravity', 'prompt', self.folder())[0], 'To fix Authentication required errors, sign in again.')

    def test_an_answer_with_refused_actions_keeps_them_in_the_receipt(self):
        with self.fake('denied_answer'):
            text, receipt = ai_cli.invoke('antigravity', 'prompt', self.folder())
        self.assertEqual((text, receipt['denied_actions']), ('Partial answer.', [{'action': 'read_url', 'display_name': 'ReadUrlContent'}]))

    def test_junk_lines_in_the_output_are_skipped(self):
        with self.fake('junk'):
            self.assertEqual(ai_cli.invoke('antigravity', 'prompt', self.folder())[0], 'Junk is skipped.')

    def test_a_call_past_the_limit_is_stopped(self):
        with self.fake('hang'), self.assertRaisesRegex(TimeoutError, 'Antigravity exceeded the 1-second call limit'):
            ai_cli.invoke('antigravity', 'prompt', self.folder(), timeout=1)

    def test_an_antigravity_job_runs_through_the_app_and_records_what_the_cli_reported(self):
        folder = self.folder()
        app = Application(str(folder), start_worker=False, antigravity_work_root=folder / 'antigravity-runtime',
                          start_readiness_check=False)
        self.addCleanup(app.store.database.close)
        app.store.save_settings({'ai_provider': 'antigravity', 'ai_model': '', 'ai_effort': ''})
        vid = app.import_text({'text': 'There are twelve garden beds.', 'title': 'Garden'})['id']
        jobs = {}
        def run(kind, mode, **extra):
            if kind not in jobs:  # a job of the same kind that is still queued blocks a second one: run the one record again
                jobs[kind] = app.submit(vid, kind, **extra)
            with self.fake(mode):
                app.ai_job(app.store.get('job', jobs[kind]), threading.Event())
            return app.store.get('video', vid)
        output = run('summary', 'ok')['outputs']['summary']  # no model chosen, and the CLI names none: the model stays "CLI default" and unknown
        self.assertEqual((output['provider'], output['model'], output['receipts'][0]['model']), ('antigravity', 'CLI default', []))
        self.assertIn('twelve garden beds', output['text'])  # the fake CLI echoes the prompt it received
        self.assertEqual(self.calls()[-1]['args'][-1], '--disable-slash-commands')
        self.assertEqual(app.last_models(), {})
        app.store.save_settings({'ai_model': 'm-one'})
        output = run('formatted', 'ok')['outputs']['formatted']
        self.assertEqual((output['model'], output['receipts'][0]['model']), ('m-one', ['m-one']))
        self.assertEqual(self.calls()[-1]['args'][-2:], ['--model', 'm-one'])
        self.assertEqual(app.last_models(), {'antigravity': 'm-one'})
        answer = run('ask', 'denied_answer', question='Anything?')['conversation'][-1]
        self.assertEqual((answer['answer'], answer['receipts'][0]['denied_actions']), ('Partial answer.', [{'action': 'read_url', 'display_name': 'ReadUrlContent'}]))
        with self.fake('signin_stderr'), self.assertRaisesRegex(RuntimeError, 'sign in again: open PowerShell, run agy'):
            app.ai_job(app.store.get('job', jobs['summary']), threading.Event())

    def test_settings_accept_antigravity_only_without_an_effort(self):
        store = Store(str(self.folder()))
        self.addCleanup(store.database.close)
        for model in ('', AGY_MODEL, 'claude-sonnet-5-5-medium', 'gpt-oss-120b-medium', 'my-own-model'):
            store.save_settings({'ai_provider': 'antigravity', 'ai_model': model})
            self.assertEqual((store.config['ai_provider'], store.config['ai_model'], store.config['ai_effort']), ('antigravity', model, ''))
        store.save_settings({'ai_provider': 'codex', 'ai_effort': 'high'})
        for data in ({'ai_provider': 'antigravity', 'ai_effort': 'high'}, {'ai_provider': 'antigravity'}):  # the second would carry the saved effort over
            with self.assertRaisesRegex(ValueError, 'Antigravity CLI has no reasoning effort'):
                store.save_settings(data)
            self.assertEqual((store.config['ai_provider'], store.config['ai_effort']), ('codex', 'high'))  # nothing was saved
        store.save_settings({'ai_provider': 'antigravity', 'ai_effort': ''})
        self.assertEqual(store.config['ai_provider'], 'antigravity')
        store.save_settings({'ai_provider': 'claude', 'ai_effort': 'max'})
        for provider in ('bard', 'gemini'):  # Gemini CLI is no longer one of the CLIs
            with self.assertRaisesRegex(ValueError, 'Select Codex, Claude, Antigravity or OpenCode'):
                store.save_settings({'ai_provider': provider})
        self.assertEqual(store.config['ai_provider'], 'claude')

    def test_each_cli_keeps_its_own_saved_model_and_effort(self):
        folder = self.folder()
        store = Store(str(folder))
        self.addCleanup(store.database.close)
        store.save_settings({'ai_provider': 'codex', 'ai_model': 'gpt-6-astra', 'ai_effort': 'low'})
        store.save_settings({'ai_provider': 'claude', 'ai_model': 'opus', 'ai_effort': 'high'})
        choices = json.loads(store.config['ai_choices'])
        self.assertEqual(choices['codex'], {'model': 'gpt-6-astra', 'effort': 'low'})
        self.assertEqual(choices['claude'], {'model': 'opus', 'effort': 'high'})
        store.save_settings({'ai_choices': '{"codex": {"model": "x", "effort": ""}}'})  # only save_settings keeps it
        self.assertEqual(json.loads(store.config['ai_choices'])['codex'], {'model': 'gpt-6-astra', 'effort': 'low'})
        with self.assertRaisesRegex(ValueError, 'Antigravity CLI has no reasoning effort'):
            store.save_settings({'ai_provider': 'antigravity', 'ai_effort': 'high'})
        self.assertNotIn('antigravity', json.loads(store.config['ai_choices']))  # a refused save remembers nothing
        reopened = Store(str(folder))
        self.addCleanup(reopened.database.close)
        self.assertEqual(json.loads(reopened.config['ai_choices'])['codex'], {'model': 'gpt-6-astra', 'effort': 'low'})
        for broken in ('not json', '[1]', '{"bard": {"model": "m"}, "claude": "opus"}'):
            reopened.config.update(ai_choices=broken, ai_provider='claude', ai_model='opus', ai_effort='high')
            reopened.save_settings({'ai_provider': 'codex', 'ai_model': 'gpt-6-astra', 'ai_effort': 'low'})
            self.assertEqual(json.loads(reopened.config['ai_choices']),
                             {'claude': {'model': 'opus', 'effort': 'high'}, 'codex': {'model': 'gpt-6-astra', 'effort': 'low'}})

    def test_a_saved_gemini_cli_falls_back_to_the_default_provider_when_the_settings_load(self):
        folder = self.folder()
        settings = {'ai_provider': 'gemini', 'ai_model': 'pro', 'ai_effort': '', 'primary': 'fetchtranscript', 'language': 'de'}
        (folder / 'settings.json').write_text(json.dumps({'settings': settings, 'keys': {}}), encoding='utf-8')
        store = Store(str(folder))
        self.addCleanup(store.database.close)
        self.assertEqual((store.config['ai_provider'], store.config['ai_model'], store.config['ai_effort']), (DEFAULTS['ai_provider'], '', ''))
        self.assertEqual((store.config['primary'], store.config['language']), ('fetchtranscript', 'de'))  # nothing else is touched
        self.assertEqual(store.public_settings()['ai_provider'], DEFAULTS['ai_provider'])
        store.save_settings({'ai_model': 'gpt-6.1-sol'})  # the settings can be saved again, and the file then holds the fallback
        saved = json.loads((folder / 'settings.json').read_text(encoding='utf-8'))['settings']
        self.assertEqual((saved['ai_provider'], saved['ai_model']), (DEFAULTS['ai_provider'], 'gpt-6.1-sol'))
        for kept in ({'ai_provider': 'claude', 'ai_model': 'opus', 'ai_effort': 'high'}, {'ai_provider': 'antigravity', 'ai_model': AGY_MODEL, 'ai_effort': ''}):
            (folder / 'settings.json').write_text(json.dumps({'settings': kept, 'keys': {}}), encoding='utf-8')
            reopened = Store(str(folder))
            self.addCleanup(reopened.database.close)
            self.assertEqual({key: reopened.config[key] for key in kept}, kept)


if __name__=='__main__':
    unittest.main()
