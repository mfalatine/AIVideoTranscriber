"""Loopback integration tests. Provider and AI responses are deterministic fixtures."""
import http.client
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_cli
import app_version
import providers
from server import Application, Server


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # /api/bootstrap lists the local AI CLIs: read their config files from this fixture folder, never the real home folder.
        real_availability = ai_cli.availability
        patcher = mock.patch.object(ai_cli, 'availability', lambda home=None: real_availability(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        # A key in the real environment would count as a configured key; these tests start without any.
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('GEMINI_API_KEY', 'SUPADATA_API_KEY', 'FETCHTRANSCRIPT_API_KEY'):
            os.environ.pop(name, None)
        def fetcher(provider, vid, key, language, **options):
            if provider == 'supadata':
                raise providers.ProviderError('rate_limit', 'Fixture rate limit')
            payload = {'video_id': vid, 'language': 'en', 'metadata': {'title': 'HTTP fixture'},
                       'segments': [{'text': 'Twelve garden beds.', 'start': 0, 'duration': 2}]}
            return {**providers.normalize(provider, payload, vid), 'elapsed_seconds': 0.01}
        def runner(provider, prompt, *args, **kwargs):
            return 'There are twelve garden beds.', {'requested_model': 'fixture'}
        self.app = Application(self.temp.name, fetcher=fetcher, ai_runner=runner,
                               antigravity_work_root=Path(self.temp.name) / 'antigravity-runtime',
                               start_readiness_check=False)
        # This fixture fails Supadata first so FetchTranscript has to answer; the app default is now Gemini first.
        self.app.store.save_settings({'primary': 'supadata', 'fallback': 'fetchtranscript'})
        self.server = Server(0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f'http://127.0.0.1:{self.port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.app.stop.set()
        self.app.worker.join(3)
        self.app.store.database.close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        supplied = {'X-AIVideoTranscriber-Token': self.app.token, 'Origin': self.origin,
                    'Content-Type': 'application/json'}
        supplied.update(headers or {})
        try:
            connection.request(method, path, None if payload is None else json.dumps(payload), supplied)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def wait_job(self, jid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = json.loads(self.request('GET', '/api/state')[2])
            job = next(j for j in result['jobs'] if j['id'] == jid)
            if job['status'] not in ('queued', 'running'):
                self.assertEqual(job['status'], 'completed', job)
                return job
            time.sleep(0.02)
        self.fail('Fixture job did not finish')

    def test_local_page_and_bootstrap_do_not_expose_api_keys(self):
        self.app.store.save_settings({'supadata_key': 'fixture-private-key'})
        status, headers, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'AI Video Transcriber', body)
        self.assertEqual(body.count(b'id="appVersion"'), 1)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        status, _, body = self.request('GET', '/api/bootstrap')
        self.assertEqual(status, 200)
        self.assertNotIn(b'fixture-private-key', body)
        boot = json.loads(body)
        self.assertEqual(boot['version'], app_version.VERSION)
        self.assertTrue(boot['settings']['keys_configured']['supadata'])
        self.assertEqual(boot['settings']['keys_saved'], {'gemini': False, 'supadata': True, 'fetchtranscript': False, 'antigravity': False})
        self.assertTrue(boot['settings']['keys_configured']['antigravity'])
        self.assertEqual(sorted(boot['ai']), ['antigravity', 'claude', 'codex', 'opencode'])
        for cli in boot['ai'].values():
            self.assertLessEqual({'installed', 'label', 'default_model', 'default_effort', 'models', 'efforts', 'last_model'}, set(cli))

    def test_health_version_uses_the_shared_version_source(self):
        status, headers, body = self.request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['version'], app_version.VERSION)
        self.assertTrue(headers['Server'].startswith(f'AIVideoTranscriber/{app_version.VERSION} '))

    def test_missing_optional_whisper_runtime_is_actionable_and_does_not_stop_http(self):
        self.app.whisper_python = None
        self.app._check_whisper_readiness()
        self.assertEqual(self.app.whisper_readiness['status'], 'not_installed')
        self.assertIn('AIVIDEOTRANSCRIBER_WHISPER_PYTHON', self.app.whisper_readiness['reason'])
        status, _, _ = self.request('GET', '/health')
        self.assertEqual(status, 200)

    def test_gemini_settings_over_http_never_return_the_key(self):
        status, _, body = self.request('POST', '/api/settings', {'gemini_key': 'fixture-gemini-key', 'gemini_model': 'gemini-3.9-flash',
                                                                 'gemini_fallback_models': 'gemini-3.8-flash\ngemini-3.6-flash',
                                                                 'gemini_clip_seconds': '1200'})
        self.assertEqual(status, 200)
        self.assertNotIn(b'fixture-gemini-key', body)
        self.assertTrue(json.loads(body)['keys_configured']['gemini'])
        boot = self.request('GET', '/api/bootstrap')[2]
        self.assertNotIn(b'fixture-gemini-key', boot)
        self.assertEqual(json.loads(boot)['settings']['gemini_model'], 'gemini-3.9-flash')
        status, _, body = self.request('POST', '/api/settings', {'gemini_model': 'bad model'})
        self.assertEqual(status, 400)
        self.assertIn('Gemini model', json.loads(body)['error'])
        status, _, body = self.request('POST', '/api/settings', {'gemini_clip_seconds': '59'})
        self.assertEqual(status, 400)
        self.assertIn('clip length', json.loads(body)['error'])

    def test_page_offers_every_service_and_the_script_only_uses_ids_that_exist(self):
        page = self.request('GET', '/')[2].decode()
        script = self.request('GET', '/app.js')[2].decode()
        def options(select):
            return re.findall(r'value="([^"]*)"', re.search(f'<select id="{select}">(.*?)</select>', page).group(1))
        self.assertEqual(options('mode'), ['auto', *providers.PROVIDERS])
        self.assertEqual(options('primary'), list(providers.PROVIDERS))
        self.assertEqual(options('fallback'), [*providers.PROVIDERS, 'none'])
        self.assertEqual(options('aiProvider'), [])  # the script fills it from the server's CLI list, so a new CLI needs no page change
        self.assertIn('choices.aiProviderChoices(boot.ai)', script)
        ids = set(re.findall(r'id="([^"]+)"', page))
        used = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", script))
        used |= set(re.findall(r"'([a-z_]+)'", re.search(r"const settingKeys = \[(.*?)\]", script).group(1)))
        used |= {f'{p}_{part}' for p in providers.KEYED_PROVIDERS for part in ('key', 'state', 'remove')} | {f'{p}KeyNote' for p in providers.KEYED_PROVIDERS}
        # The model selects are read by id through chosenModel('select', 'custom box').
        pairs = re.findall(r"chosenModel\('([a-z_]+)','([a-z_]+)'\)", script)
        self.assertEqual(len(pairs), 2)
        used |= {name for pair in pairs for name in pair}
        self.assertEqual(sorted(used - ids), [])
        self.assertIn('https://aistudio.google.com/api-keys', page)
        for p in providers.PROVIDERS:
            self.assertIn(f"{p}:'", script)

    def test_page_scripts_load_before_the_app_and_are_all_served(self):
        page = self.request('GET', '/')[2].decode()
        scripts = re.findall(r'<script src="/([a-z]+\.js)" defer></script>', page)
        self.assertEqual(scripts, ['activity.js', 'choices.js', 'markdown.js', 'urls.js', 'selection.js', 'readaloud.js', 'app.js'])
        for name in scripts:
            with self.subTest(script=name):
                status, headers, body = self.request('GET', '/' + name)
                self.assertEqual(status, 200)
                self.assertIn('javascript', headers['Content-Type'])
                self.assertGreater(len(body), 500)
        self.assertNotIn('Process all', page)
        self.assertIn('Get transcripts', page)

    def test_favicon_is_local_and_served_without_a_404(self):
        page = self.request('GET', '/')[2].decode()
        self.assertIn('rel="icon" href="/favicon.svg"', page)
        status, headers, body = self.request('GET', '/favicon.svg')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/svg+xml')
        self.assertIn(b'<svg', body)

    def test_add_videos_box_wraps_long_lines_and_library_titles_stay_on_one_line(self):
        css = self.request('GET', '/style.css')[2].decode()
        rule = re.search(r'\.batch-panel textarea\{([^}]*)\}', css).group(1)
        self.assertIn('white-space:pre-wrap', rule)
        self.assertIn('overflow-wrap:anywhere', rule)
        name = re.search(r'\.video-row \.video-name\{([^}]*)\}', css).group(1)
        self.assertIn('text-overflow:ellipsis', name)
        self.assertIn('white-space:nowrap', name)

    def test_top_tabs_replace_the_sidebar_and_keep_the_transcript_workspace_below(self):
        page = self.request('GET', '/')[2].decode()
        css = self.request('GET', '/style.css')[2].decode()
        script = self.request('GET', '/app.js')[2].decode()
        self.assertNotIn('PERSONAL TRANSCRIPT WORKSPACE', page)
        self.assertNotIn('class="sidebar"', page)
        tabs = re.findall(r'data-top-tab="([^"]+)"', page)
        self.assertEqual(tabs, ['add', 'library', 'activity'])
        self.assertRegex(page, r'id="top-panel-add"[^>]*aria-labelledby="top-tab-add"(?![^>]* hidden)')
        for name in ('library', 'activity'):
            self.assertRegex(page, rf'id="top-panel-{name}"[^>]*aria-labelledby="top-tab-{name}"[^>]* hidden')
        self.assertLess(page.index('class="top-panels"'), page.index('class="workspace"'))
        self.assertNotRegex(page, r'<h2>(Add videos|Library|Activity)</h2>|>BATCH<')
        self.assertIn('<span class="document-label">YouTube Title:</span>', page)
        self.assertIn('<span class="document-label">Transcript by:</span>', page)
        self.assertNotIn('<h1 id="videoTitle"', page)
        self.assertIn("topTab = 'add'", script)
        self.assertIn("localStorage.getItem(topTabKey)", script)
        self.assertIn("localStorage.setItem(topTabKey,topTab)", script)
        self.assertNotRegex(script, r'topPanelOpen|topPanelOpenKey|folded')
        self.assertIn('body{width:100%;min-height:100vh;overflow-x:clip', css)
        self.assertIn('.terminal-content,.conversation{flex:0 1 auto;height:auto;min-height:120px;max-height:var(--split-bottom-max);overflow:auto}', css)
        self.assertIn('@media(max-width:899px){.empty{min-height:0}', css)
        self.assertNotIn('.top-panels.folded', css)

    def test_cross_origin_invalid_token_and_host_are_rejected(self):
        for headers in [{'Origin': 'https://untrusted.example'},
                        {'X-AIVideoTranscriber-Token': ''}, {'Host': 'untrusted.example'}]:
            with self.subTest(headers=headers):
                status, _, _ = self.request('POST', '/api/import', {'text': 'Do not save'}, headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.app.store.list('video'), [])
        self.assertEqual(self.request('GET', '/api/state', headers={'Host':'untrusted.example'})[0], 403)

    def test_gemini_models_route_needs_the_page_token_and_a_saved_key(self):
        asked = []
        self.app.model_lister = lambda key: asked.append(key) or []
        self.request('POST', '/api/settings', {'gemini_key': 'fixture-gemini-key'})
        for headers in [{'Origin': 'https://untrusted.example'}, {'X-AIVideoTranscriber-Token': ''}, {'Host': 'untrusted.example'}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request('POST', '/api/gemini-models', {}, headers)[0], 403)
        self.assertEqual(asked, [])
        self.app.store.save_settings({'clear_gemini': True})
        status, _, body = self.request('POST', '/api/gemini-models', {})
        self.assertEqual((status, json.loads(body)), (400, {'error': 'Save a Gemini key first.'}))
        self.assertEqual(asked, [])

    def test_gemini_models_are_listed_kept_ten_minutes_and_refreshed_over_http(self):
        asked = []
        models = [{'id': 'gemini-3.8-flash', 'label': 'Gemini 3.8 Flash', 'input_tokens': 1048576, 'output_tokens': 65536}]
        self.app.model_lister = lambda key: asked.append(key) or models
        self.request('POST', '/api/settings', {'gemini_key': 'fixture-gemini-key'})
        for refresh, asked_so_far in [(False, 1), (False, 1), (True, 2), (False, 2), (None, 2)]:
            with self.subTest(refresh=refresh):
                status, _, body = self.request('POST', '/api/gemini-models', {} if refresh is None else {'refresh': refresh})
                self.assertEqual((status, json.loads(body)), (200, {'models': models}))
                self.assertNotIn(b'fixture-gemini-key', body)
                self.assertEqual(len(asked), asked_so_far)
        self.assertEqual(set(asked), {'fixture-gemini-key'})

    def test_antigravity_models_route_needs_the_page_token_and_runs_the_list(self):
        asked, listing = [], {'models': [{'id': 'm-one', 'label': 'Model One'}], 'error': ai_cli.ANTIGRAVITY_SIGN_IN}
        with mock.patch.object(ai_cli, 'antigravity_model_list', lambda clock=None, run=True: asked.append(run) or listing):
            for headers in [{'Origin': 'https://untrusted.example'}, {'X-AIVideoTranscriber-Token': ''}, {'Host': 'untrusted.example'}]:
                with self.subTest(headers=headers):
                    self.assertEqual(self.request('POST', '/api/antigravity-models', {}, headers)[0], 403)
            self.assertEqual(self.request('GET', '/api/bootstrap')[0], 200)
            self.assertEqual(asked, [])
            status, _, body = self.request('POST', '/api/antigravity-models', {})
        self.assertEqual((status, json.loads(body), asked), (200, listing, [True]))

    def test_gemini_model_errors_are_scrubbed_and_never_use_the_expired_page_token_status(self):
        self.request('POST', '/api/settings', {'gemini_key': 'fixture-gemini-key'})
        failure = []
        def lister(key):
            if failure:
                raise providers.ProviderError(failure[0], 'Google said no to fixture-gemini-key.')
            return [{'id': 'gemini-3.8-flash', 'label': 'gemini-3.8-flash', 'input_tokens': 1}]
        self.app.model_lister = lister
        for code, expected in [('authentication', 400), ('invalid_request', 400), ('rate_limit', 429), ('connection', 502), ('provider_error', 502)]:
            with self.subTest(code=code):
                failure[:] = [code]
                status, _, body = self.request('POST', '/api/gemini-models', {'refresh': True})
                self.assertEqual(status, expected)
                self.assertNotIn(status, (401, 403))
                self.assertEqual(json.loads(body), {'error': 'Google said no to [API key redacted].'})
                self.assertNotIn(b'fixture-gemini-key', body)
        failure[:] = []  # a failure is not remembered
        status, _, body = self.request('POST', '/api/gemini-models', {})
        self.assertEqual((status, [m['id'] for m in json.loads(body)['models']]), (200, ['gemini-3.8-flash']))

    def test_static_allowlist_protects_library_and_source_files(self):
        for path in ['/data/settings.json', '/server.py', '/../storage.py', '/%2e%2e/storage.py']:
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path)[0], 404)

    def test_activity_module_is_served_and_large_queue_is_not_truncated(self):
        status, _, body = self.request('GET', '/activity.js')
        self.assertEqual(status, 200)
        self.assertIn(b'describeActivity', body)
        for index in range(70):
            self.app.store.put('job', str(index), {'id': str(index), 'status': 'queued', 'config': {'private': 'hidden'}})
        state = json.loads(self.request('GET', '/api/state')[2])
        self.assertEqual(len(state['jobs']), 70)
        self.assertTrue(all('config' not in job for job in state['jobs']))

    def test_batch_fallback_ai_and_exports_through_http(self):
        status, _, body = self.request('POST', '/api/batch', {'urls': 'jNQXAC9IVRw\niG9CE55wbtY'})
        self.assertEqual(status, 200)
        for jid in json.loads(body)['jobs']:
            self.wait_job(jid)
        video = json.loads(self.request('GET', '/api/video?id=jNQXAC9IVRw')[2])
        self.assertEqual(video['selected_provider'], 'fetchtranscript')
        self.assertEqual([a['status'] for a in video['attempts']], ['failed', 'success'])
        self.assertNotIn('raw_response', video['sources']['fetchtranscript'])
        result = json.loads(self.request('POST', '/api/ai', {'id': video['id'], 'kind': 'summary'})[2])
        self.wait_job(result['job_id'])
        raw = self.request('GET', '/api/export?id=jNQXAC9IVRw&tab=raw')[2]
        summary = self.request('GET', '/api/export?id=jNQXAC9IVRw&tab=summary')[2]
        self.assertIn(b'Twelve garden beds.', raw)
        self.assertIn(b'There are twelve garden beds.', summary)
        data = json.loads(self.request('GET', '/api/export?id=jNQXAC9IVRw&tab=json')[2])
        self.assertIn('raw_response', data['sources']['fetchtranscript'])
        with zipfile.ZipFile(io.BytesIO(self.request('GET', '/api/export-all')[2])) as archive:
            self.assertIn('all-transcripts.md', archive.namelist())
            self.assertEqual(archive.read('all-transcripts.md').count(b'Twelve garden beds.'), 2)

    def test_unexpected_source_failure_falls_back_over_http_without_disclosing_details(self):
        normal = self.app.fetcher
        def broken(provider, *args, **options):
            if provider == 'antigravity':
                raise ValueError('bad float duration: fixture-sensitive-detail')
            return normal(provider, *args, **options)
        self.app.fetcher = broken
        self.app.store.save_settings({'primary': 'antigravity', 'fallback': 'fetchtranscript'})
        response = json.loads(self.request('POST', '/api/batch', {'urls': 'abcdefghijk'})[2])
        self.wait_job(response['jobs'][0])
        body = self.request('GET', '/api/video?id=abcdefghijk')[2]
        self.assertNotIn(b'fixture-sensitive-detail', body)
        video = json.loads(body)
        self.assertEqual(video['selected_provider'], 'fetchtranscript')
        self.assertEqual([(attempt['status'], attempt.get('code')) for attempt in video['attempts']],
                         [('failed', 'provider_error'), ('success', None)])

    def test_http_refetch_is_explicit_and_keeps_the_existing_record_until_success(self):
        first = json.loads(self.request('POST', '/api/batch', {'urls': 'abcdefghijk'})[2])
        self.wait_job(first['jobs'][0])
        reused = json.loads(self.request('POST', '/api/batch', {'urls': 'abcdefghijk'})[2])
        self.assertEqual((reused['jobs'], reused['reused']), ([], 1))
        again = json.loads(self.request('POST', '/api/batch', {'urls': 'abcdefghijk', 'refetch': True})[2])
        self.assertEqual(again['reused'], 0)
        self.wait_job(again['jobs'][0])
        video = json.loads(self.request('GET', '/api/video?id=abcdefghijk')[2])
        self.assertEqual(len(video['attempts']), 4)  # Supadata failure + FetchTranscript success, twice
        self.assertEqual(video['sources']['fetchtranscript']['text'], 'Twelve garden beds.')

    def test_one_word_paste_is_not_sent_to_ai(self):
        status, _, body = self.request('POST', '/api/import', {'url': 'jNQXAC9IVRw', 'text': 'h'})
        self.assertEqual(status, 200)
        vid = json.loads(body)['id']
        status, _, body = self.request('POST', '/api/ai', {'id': vid, 'kind': 'summary'})
        self.assertEqual(status, 400)
        self.assertIn('full transcript', json.loads(body)['error'])
        self.assertEqual(self.app.store.list('job'), [])

    def test_saved_video_can_get_transcript_again_with_chosen_route_without_duplication(self):
        first = json.loads(self.request('POST', '/api/batch', {'urls': 'jNQXAC9IVRw', 'mode': 'auto'})[2])
        self.wait_job(first['jobs'][0])
        video = self.app.store.get('video', 'jNQXAC9IVRw')
        video['outputs']['summary'] = {'text': 'Existing output stays.', 'source_hash': video['sources']['fetchtranscript']['hash']}
        self.app.store.put('video', video['id'], video)
        status, _, body = self.request('POST', '/api/batch', {
            'urls': 'https://youtu.be/jNQXAC9IVRw', 'mode': 'fetchtranscript', 'compare': False, 'force': True})
        result = json.loads(body)
        self.assertEqual((status, result['reused'], len(result['jobs'])), (200, 0, 1))
        job = self.app.store.get('job', result['jobs'][0])
        self.assertEqual((job['mode'], job['rerun']), ('fetchtranscript', True))
        self.wait_job(job['id'])
        saved = self.app.store.get('video', 'jNQXAC9IVRw')
        self.assertEqual(len(self.app.store.list('video')), 1)
        self.assertEqual(saved['selected_provider'], 'fetchtranscript')
        self.assertEqual(saved['outputs']['summary']['text'], 'Existing output stays.')
        page = self.request('GET', '/')[2]
        self.assertIn(b'id="retrieveCurrent"', page)
        self.assertIn(b'Get transcript', page)

    def test_malformed_requests_return_actionable_errors(self):
        for path, payload in [('/api/import', []), ('/api/import', {'text':''}),
                              ('/api/batch', {'urls':'https://example.com/private'}),
                              ('/api/ai', {'id':'missing', 'kind':'summary'})]:
            with self.subTest(path=path, payload=payload):
                status, _, body = self.request('POST', path, payload)
                if path == '/api/batch':
                    self.assertEqual(status, 200)
                    self.assertEqual(len(json.loads(body)['errors']), 1)
                else:
                    self.assertEqual(status, 400)
                    self.assertIn('error', json.loads(body))


if __name__ == '__main__':
    unittest.main()
