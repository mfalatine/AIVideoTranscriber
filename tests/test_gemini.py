"""Gemini client tests. Every reply is a fixture; nothing here calls the real network."""
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gemini
import providers

VID = 'jNQXAC9IVRw'
KEY = 'AIzaFixtureKeyNotReal0123456789abcd'
VIDEO = 39600  # video tokens of every clip in these fixtures unless a test says otherwise
FULL = 15000  # audio tokens of a full 600-second clip: 54,600 media tokens with the video
SHORT = 5000  # audio tokens of a clip in which the video ended: 44,600 media tokens, under 90%
BEYOND_END = 'start_offset is beyond the video duration'  # explicit semantic fixture, not a claimed live message
GENERIC_400 = 'Request contains an invalid argument.'
HIGH_DEMAND = ('gemini-3.8-flash is currently experiencing high demand, spikes in demand are usually temporary. '
               'Please try again later.')


class Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.headers = {}
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.body[:limit]


def usage(audio, modality='audio', video=VIDEO):
    """Token usage as Google reports it. audio=None leaves audio out (live replies counted it inside
    video); video=None and audio=None leave out every media count."""
    entries = [{'modality': 'text', 'tokens': 400}]
    if video is not None:
        entries.append({'modality': 'video', 'tokens': video})
    if audio is not None:
        entries.append({'modality': modality, 'tokens': audio})
    return {'input_tokens_by_modality': entries, 'total_input_tokens': 400 + (video or 0) + (audio or 0), 'total_output_tokens': 120}


def completed(answer_text, audio=FULL, modality='audio', status='completed', video=VIDEO):
    return Response({'status': status, 'usage': usage(audio, modality, video),
                     'steps': [{'type': 'thought', 'content': []},
                               {'type': 'model_output', 'content': [{'type': 'text', 'text': answer_text}]}]})


def reply(segments=(), audio=FULL, language='en', modality='audio', video=VIDEO, ended=None):
    answer = {'language': language, 'segments': [{'start': s, 'text': t} for s, t in segments]}
    if ended is not None:
        answer['video_ends_in_this_clip'] = ended
    return completed(json.dumps(answer), audio, modality, video=video)


def http_error(status, message='Fixture failure', headers=None, details=None):
    body = json.dumps({'error': {'code': status, 'message': message, 'status': 'FIXTURE',
                                 **({'details': details} if details is not None else {})}}).encode()
    return urllib.error.HTTPError(gemini.ENDPOINT, status, 'Fixture', headers or {}, io.BytesIO(body))


class Fake:
    """Scripted opener. Each Gemini POST takes the next reply; once the script is used up, it answers
    with an explicit semantic past-end HTTP 400 fixture. oEmbed requests are answered separately."""
    def __init__(self, *replies, oembed=None):
        self.replies = list(replies)
        self.oembed = {'title': 'Fixture title'} if oembed is None else oembed
        self.requests, self.oembed_requests = [], []

    def __call__(self, request, timeout=None):
        if request.full_url.startswith(gemini.OEMBED):
            self.oembed_requests.append((request, timeout))
            if isinstance(self.oembed, BaseException):
                raise self.oembed
            return self.oembed if isinstance(self.oembed, Response) else Response(self.oembed)
        self.requests.append((request, timeout))
        item = self.replies.pop(0) if self.replies else http_error(400, BEYOND_END)
        item = item() if callable(item) else item
        if isinstance(item, BaseException):
            raise item
        return item

    def bodies(self):
        return [json.loads(request.data) for request, _ in self.requests]


class Sleeper:
    def __init__(self, hook=None):
        self.calls, self.hook = [], hook

    def __call__(self, seconds):
        self.calls.append(seconds)
        if self.hook:
            self.hook()


def run(fake, **options):
    options.setdefault('sleep', Sleeper())
    options.setdefault('clip_seconds', 600)  # preserve the original slicing fixtures; product default is tested separately
    options.setdefault('duration_reader', lambda vid, cancel: None)  # focused fixtures never invoke yt-dlp
    return gemini.fetch(VID, KEY, opener=fake, **options)


class RequestTests(unittest.TestCase):
    def test_request_shape_and_key_only_in_header(self):
        fake = Fake(reply([(1, 'a')]), reply([(601, 'b')], audio=SHORT))
        run(fake)
        first, timeout = fake.requests[0]
        self.assertEqual((first.full_url, first.get_method(), timeout), (gemini.ENDPOINT, 'POST', 300))
        self.assertEqual(first.full_url, 'https://generativelanguage.googleapis.com/v1beta/interactions')
        self.assertEqual(first.get_header('X-goog-api-key'), KEY)
        self.assertEqual(first.get_header('Content-type'), 'application/json')
        self.assertEqual(first.get_header('Accept'), 'application/json')
        self.assertEqual(first.get_header('User-agent'), f'AIVideoTranscriber/{gemini.app_version.VERSION}')
        for request, _ in fake.requests:
            self.assertNotIn(KEY, request.full_url)
            self.assertNotIn(KEY, request.data.decode())
        body = fake.bodies()[0]
        self.assertEqual(body['model'], 'gemini-3.8-flash')
        self.assertIs(body['store'], False)
        video, text = body['input']
        self.assertEqual(video['type'], 'video')
        self.assertEqual(video['uri'], f'https://www.youtube.com/watch?v={VID}')
        self.assertEqual(video['processing'], {'type': 'static', 'start_offset': '0s', 'end_offset': '600s'})
        self.assertEqual(video['resolution'], 'low')
        self.assertEqual(text['type'], 'text')
        self.assertEqual(text['text'], gemini.PROMPT)
        self.assertNotRegex(text['text'], r'\d')  # no clip times: live, they made Gemini stop early
        self.assertEqual(body['response_format']['type'], 'text')
        self.assertEqual(body['response_format']['mime_type'], 'application/json')
        self.assertEqual(body['response_format']['schema'], gemini.SCHEMA)
        self.assertNotIn('temperature', first.data.decode())
        second = fake.bodies()[1]['input']
        self.assertEqual(second[0]['processing'], {'type': 'static', 'start_offset': '600s', 'end_offset': '1200s'})
        self.assertEqual(second[1]['text'], gemini.PROMPT)

    def test_default_clip_is_twenty_minutes_and_listed_output_limit_is_sent(self):
        fake = Fake(reply([(1, 'whole video')], ended=True))
        gemini.fetch(VID, KEY, opener=fake,
                     model_limits={gemini.DEFAULT_MODEL: {'output_tokens': 65536}}, sleep=Sleeper(),
                     duration_reader=lambda vid, cancel: 1200)
        body = fake.bodies()[0]
        self.assertEqual(body['input'][0]['processing'], {'type': 'static', 'start_offset': '0s', 'end_offset': '1200s'})
        self.assertEqual(body['generation_config'], {'max_output_tokens': 65536})
        self.assertEqual(len(fake.requests), 1)

    def test_model_option_missing_key_and_bad_input_never_reach_the_network(self):
        fake = Fake(reply([(1, 'a')], audio=SHORT))
        run(fake, model='gemini-custom-1')
        self.assertEqual(fake.bodies()[0]['model'], 'gemini-custom-1')
        refuse = Fake()
        with self.assertRaises(providers.ProviderError) as found:
            providers.fetch('gemini', VID, '', opener=refuse)
        self.assertEqual(found.exception.code, 'missing_key')
        for vid, key, model, code in [('short', KEY, None, 'invalid_request'), (VID, KEY, 'bad model!', 'invalid_request'),
                                      (VID, 'key\nwith-newline', None, 'authentication'), (VID, 'k’ey', None, 'authentication')]:
            with self.subTest(vid=vid, model=model), self.assertRaises(providers.ProviderError) as found:
                gemini.fetch(vid, key, model=model, opener=refuse)
            self.assertEqual(found.exception.code, code)
        self.assertEqual(refuse.requests, [])


class MetadataDurationTests(unittest.TestCase):
    def test_yt_dlp_is_isolated_bounded_and_uses_the_selected_python(self):
        process = mock.Mock()
        process.poll.return_value = 0
        process.returncode = 0
        process.stdout = io.StringIO('904\n')
        with mock.patch.object(gemini, 'YTDLP_PYTHON', Path(sys.executable)), \
                mock.patch.object(gemini.subprocess, 'Popen', return_value=process) as opened:
            self.assertEqual(gemini.youtube_duration(VID), 904)
        command = opened.call_args.args[0]
        self.assertEqual(command, [str(Path(sys.executable)), '-I', '-B', '-c', gemini.YTDLP_BOOTSTRAP,
                                   '--ignore-config', '--no-cache-dir',
                                   '--no-playlist', '--skip-download', '--print', 'duration',
                                   f'https://www.youtube.com/watch?v={VID}'])
        self.assertEqual(opened.call_args.kwargs['stdin'], gemini.subprocess.DEVNULL)
        self.assertEqual(opened.call_args.kwargs['stderr'], gemini.subprocess.DEVNULL)
        self.assertEqual(opened.call_args.kwargs['creationflags'], getattr(gemini.subprocess, 'CREATE_NO_WINDOW', 0))

    def test_missing_optional_duration_interpreter_does_not_block_transcription(self):
        with mock.patch.object(gemini, 'YTDLP_PYTHON', None), \
                mock.patch.object(gemini, '_YTDLP_SETUP_MESSAGE_REPORTED', False), \
                self.assertLogs('gemini', level='WARNING') as logged, \
                mock.patch.object(gemini.subprocess, 'Popen') as opened:
            self.assertIsNone(gemini.youtube_duration(VID))
        opened.assert_not_called()
        self.assertIn('AIVIDEOTRANSCRIBER_YTDLP_PYTHON', logged.output[0])

    def test_yt_dlp_timeout_kills_the_hidden_process_and_returns_no_duration(self):
        class Hanging:
            def __init__(self):
                self.stdout = io.StringIO('')
                self.returncode = None
                self.killed = False

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                if self.killed:
                    self.returncode = -9
                    return self.returncode
                raise subprocess.TimeoutExpired('yt-dlp', timeout)

            def kill(self):
                self.killed = True

        process = Hanging()
        with mock.patch.object(gemini, 'YTDLP_PYTHON', Path(sys.executable)), \
             mock.patch.object(gemini.subprocess, 'Popen', return_value=process), \
             mock.patch.object(gemini.time, 'monotonic', side_effect=[100, 100 + gemini.METADATA_TIMEOUT + 1]):
            self.assertIsNone(gemini.youtube_duration(VID))
        self.assertTrue(process.killed)

    def test_invalid_id_and_cancellation_never_start_yt_dlp(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(gemini.subprocess, 'Popen') as opened:
            self.assertIsNone(gemini.youtube_duration('bad id'))
            with self.assertRaises(InterruptedError):
                gemini.youtube_duration(VID, cancel)
        opened.assert_not_called()

    def test_duration_parser_rejects_junk_and_unsafe_bounds(self):
        for value in (None, '', 'not-a-number', True, float('nan'), float('inf'), 0, -1,
                      gemini.MAX_VIDEO_SECONDS + 1):
            with self.subTest(value=value):
                self.assertIsNone(gemini.parse_video_duration(value))
        self.assertEqual(gemini.parse_video_duration('904.25'), 904.25)

    def test_duration_is_retried_once_before_the_first_google_request(self):
        calls = []
        def reader(vid, cancel):
            calls.append((vid, len(fake.requests)))
            return None if len(calls) == 1 else 904
        fake = Fake(reply([(1, 'whole video')], ended=True))
        result = gemini.fetch(VID, KEY, opener=fake, sleep=Sleeper(), duration_reader=reader)
        self.assertEqual(calls, [(VID, 0), (VID, 0)])
        self.assertEqual((len(fake.requests), result['duration_source'], result['request_counts']),
                         (1, 'yt_dlp', {'gemini_api': 1, 'youtube_metadata': 2}))


class SlicingTests(unittest.TestCase):
    def test_timing_callbacks_cover_clip_sizes_once_across_retries_and_model_switches(self):
        known = lambda vid, cancel: 1250
        starts, finished = [], []
        fake = Fake(reply([(1, 'one')], ended=True), reply([(601, 'two')], ended=True),
                    reply([(1201, 'three')], audio=SHORT, ended=True))
        run(fake, duration_reader=known,
            step_started=lambda *values: starts.append(values),
            step_completed=lambda *values: finished.append(values))
        self.assertEqual([(item[0], item[1], item[2], item[4]) for item in starts],
                         [(1, 3, 600, [600, 50]), (2, 3, 600, [50]), (3, 3, 50, [])])
        self.assertEqual(len(finished), 3)

        for first_reply, model in ((http_error(500, 'fixture transient failure'), 'gemini-a'),
                                   (http_error(503, HIGH_DEMAND), 'gemini-b')):
            starts, finished, changed = [], [], []
            fake = Fake(first_reply, reply([(1, 'one')], ended=True))
            run(fake, model='gemini-a', fallback_models=['gemini-b'], duration_reader=lambda vid, cancel: 600,
                step_started=lambda *values: starts.append(values),
                step_completed=lambda *values: finished.append(values),
                step_model_changed=lambda *values: changed.append(values))
            self.assertEqual(len(starts), 1)
            self.assertEqual(len(finished), 1)
            self.assertEqual(finished[0][0], model)
            self.assertEqual(changed, [] if model == 'gemini-a' else [('gemini-b', 600, [])])

    def test_904_second_video_is_one_exact_clip_and_timestamps_fit_the_real_end(self):
        fake = Fake(reply([(7.4, 'start'), (1000.75, 'remembering'), (1456.76, 'thank you')],
                          audio=None, video=26761, ended=True))
        result = gemini.fetch(VID, KEY, opener=fake, sleep=Sleeper(),
                              duration_reader=lambda vid, cancel: 904)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.bodies()[0]['input'][0]['processing'],
                         {'type': 'static', 'start_offset': '0s', 'end_offset': '904s'})
        self.assertEqual((result['clips'], result['covered_seconds'], result['end_detected_by']),
                         (1, [0, 904.0], 'youtube_duration'))
        self.assertLessEqual(result['last_timestamp'], 904)
        self.assertLessEqual(result['segments'][-1]['start'], 904)
        self.assertEqual(result['raw_response']['clips'][0]['duration_seconds'], 904.0)
        self.assertEqual(result['request_counts'], {'gemini_api': 1, 'youtube_metadata': 1})

    def test_known_nonmultiple_duration_plans_exact_clips_and_never_probes_past_end(self):
        fake = Fake(reply([(1, 'one')], ended=True), reply([(601, 'two')], ended=True),
                    reply([(1201, 'three')], audio=SHORT, ended=True))
        result = run(fake, duration_reader=lambda vid, cancel: 1250)
        self.assertEqual([(b['input'][0]['processing']['start_offset'], b['input'][0]['processing']['end_offset'])
                          for b in fake.bodies()], [('0s', '600s'), ('600s', '1200s'), ('1200s', '1250s')])
        self.assertEqual([c['duration_seconds'] for c in result['raw_response']['clips']], [600, 600, 50.0])
        self.assertEqual((result['clips'], result['requests'], result['covered_seconds'], result['end_detected_by']),
                         (3, 3, [0, 1250.0], 'youtube_duration'))

    def test_model_end_flag_is_ignored_when_duration_is_unknown(self):
        fake = Fake(reply([(1, 'one')], ended=True), reply([(601, 'two')], audio=SHORT, ended=True))
        starts, completed = [], []
        result = run(fake, step_started=lambda *values: starts.append(values),
                     step_completed=lambda *values: completed.append(values))
        self.assertEqual((len(fake.requests), result['clips'], result['end_detected_by']),
                         (2, 2, 'clip_shorter_than_first'))
        self.assertEqual([(item[0], item[1], item[2]) for item in starts],
                         [(1, None, None), (2, None, None)])
        self.assertEqual(completed, [])

    def test_metadata_timeout_or_invalid_output_uses_token_fallback_and_reports_it(self):
        def timed_out(vid, cancel):
            raise subprocess.TimeoutExpired('yt-dlp', gemini.METADATA_TIMEOUT)
        for reader in (timed_out, lambda vid, cancel: 'junk'):
            progress = []
            result = run(Fake(reply([(1, 'one')]), reply([(601, 'two')], audio=SHORT)),
                         duration_reader=reader, progress=progress.append)
            self.assertEqual((result['duration_source'], result['end_detected_by']),
                             ('clip_evidence', 'clip_shorter_than_first'))
            self.assertIn('exact YouTube duration unavailable', progress[0])
            self.assertEqual(result['request_counts'], {'gemini_api': 2, 'youtube_metadata': 2})

    def test_empty_unknown_length_reply_does_not_certify_a_partial_transcript(self):
        bare = dict(audio=None, video=None)
        fake = Fake(reply([(1, 'spoken')]), reply([], **bare), http_error(401, 'not an EOF'))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake)
        self.assertEqual((found.exception.code, len(fake.requests)), ('authentication', 3))

    def test_three_clips_end_on_a_clip_shorter_than_the_first(self):
        fake = Fake(reply([(1.0, 'one'), (30.5, 'two')]), reply([(601.0, 'three')]), reply([(1201.0, 'four')], audio=SHORT))
        result = run(fake)
        self.assertEqual(len(fake.requests), 3)
        # The last clip holds 44,600 of the first clip's 54,600 media tokens: 600 x 44600 / 54600 = 490.1 s.
        self.assertEqual((result['clips'], result['requests'], result['end_detected_by'], result['covered_seconds']),
                         (3, 3, 'clip_shorter_than_first', [0, 1690.1]))
        self.assertEqual([s['text'] for s in result['segments']], ['one', 'two', 'three', 'four'])
        self.assertEqual([s['start'] for s in result['segments']], [1.0, 30.5, 601.0, 1201.0])
        self.assertEqual([s['duration'] for s in result['segments']], [29.5, 570.5, 600.0, 489.1])
        self.assertEqual(result['text'], 'one\ntwo\nthree\nfour')
        self.assertTrue(result['timed_text'].startswith('[00:00:01] one\n[00:00:30] two'))
        self.assertEqual((result['words'], result['characters'], result['last_timestamp']), (4, 18, 1690.1))
        clips = result['raw_response']['clips']
        self.assertEqual([c['media_tokens'] for c in clips], [VIDEO + FULL, VIDEO + FULL, VIDEO + SHORT])
        self.assertEqual([c['duration_seconds'] for c in clips], [600, 600, 490.1])
        self.assertEqual([(c['start'], c['end'], c['http_status']) for c in clips],
                         [(0, 600, 200), (600, 1200, 200), (1200, 1800, 200)])
        self.assertEqual(result['raw_response']['model'], 'gemini-3.8-flash')
        self.assertEqual((result['model'], result['input_tokens'], result['output_tokens']), ('gemini-3.8-flash', 3 * 40000 + 2 * FULL + SHORT, 360))

    def test_later_clip_400_ends_the_transcript(self):
        fake = Fake(reply([(1, 'a')]), reply([(601, 'b')]), http_error(400, 'start_offset is beyond the video duration'))
        result = run(fake)
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual((result['clips'], result['requests'], result['end_detected_by'], result['covered_seconds']),
                         (2, 3, 'http_400', [0, 1200]))
        self.assertIn('beyond the video duration', result['end_reason'])
        self.assertEqual((result['end_confirmed'], result['completion_warning']),
                         (False, gemini.END_NOT_CONFIRMED))

    def test_generic_later_400_is_an_error_not_end_evidence(self):
        fake = Fake(reply([(1, 'partial')]), http_error(400, GENERIC_400))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake)
        self.assertEqual((found.exception.code, found.exception.status, len(fake.requests)),
                         ('invalid_request', 400, 2))
        self.assertIn(GENERIC_400, str(found.exception))

    def test_structured_start_offset_out_of_range_is_end_evidence(self):
        detail = [{'@type': 'type.googleapis.com/google.rpc.ErrorInfo',
                   'reason': 'START_OFFSET_OUT_OF_RANGE'}]
        result = run(Fake(reply([(1, 'complete')]), http_error(400, 'bad range', details=detail)))
        self.assertEqual(result['end_detected_by'], 'http_400')

    def test_400_on_the_first_clip_is_a_real_failure(self):
        fake = Fake(http_error(400, 'Video is private'), reply([(1, 'a')]))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake)
        self.assertEqual((found.exception.code, found.exception.status), ('invalid_request', 400))
        self.assertIn('Video is private', str(found.exception))
        self.assertEqual(len(fake.requests), 1)

    def test_silent_clip_with_full_tokens_does_not_end_the_transcript(self):
        fake = Fake(reply([(2, 'a')]), reply([]), reply([(1300, 'b')], audio=SHORT))
        result = run(fake)
        self.assertEqual((len(fake.requests), result['clips'], result['end_detected_by']), (3, 3, 'clip_shorter_than_first'))
        self.assertEqual([s['text'] for s in result['segments']], ['a', 'b'])

    def test_a_short_first_clip_still_asks_for_the_next(self):
        # Without a full clip to compare with, only Google's 400 for the next clip ends a short video.
        fake = Fake(reply([(1, 'a')], audio=SHORT))
        result = run(fake)
        self.assertEqual((len(fake.requests), result['clips'], result['end_detected_by']), (2, 1, 'http_400'))
        self.assertEqual(result['end_reason'], f'Gemini rejected the request: {BEYOND_END}')

    def test_threshold_is_90_percent_of_the_first_clip_and_modality_case_is_ignored(self):
        # First clip 54,600 media tokens; 90% is 49,140 = 39,600 video + 9,540 audio.
        fake = Fake(reply([(1, 'a')], modality='AUDIO'), reply([(601, 'b')], audio=9540, modality='AUDIO'),
                    reply([(1201, 'c')], audio=9539, modality='Audio'))
        result = run(fake)
        self.assertEqual((result['clips'], result['end_detected_by']), (3, 'clip_shorter_than_first'))
        self.assertEqual([c['media_tokens'] for c in result['raw_response']['clips']], [54600, 49140, 49139])

    def test_media_token_baseline_is_per_model_after_a_switch(self):
        # Model B's full clip has 41,487 media tokens. Compared with model A's 54,600 it would
        # falsely report an end at 2,111.8 seconds; B must establish its own baseline instead.
        fake = Fake(reply([(1, 'a full')]), http_error(503, HIGH_DEMAND),
                    reply([(1201, 'b full')], audio=1887),
                    reply([(2401, 'b short')], audio=500, video=10000))
        result = run(fake, model='gemini-a', fallback_models=['gemini-b'], clip_seconds=1200)
        self.assertEqual([body['model'] for body in fake.bodies()],
                         ['gemini-a', 'gemini-a', 'gemini-b', 'gemini-b'])
        self.assertEqual((result['clips'], result['end_detected_by']), (3, 'clip_shorter_than_first'))
        self.assertEqual(result['raw_response']['clips'][1]['duration_seconds'], 1200)
        self.assertNotEqual(result['covered_seconds'], [0, 2111.8])

    def test_video_tokens_alone_measure_the_clips_as_in_the_live_replies(self):
        # Live 2026-10-14 (Stanford speech): Google reported only video tokens, 52,801 for the full first
        # clip and 26,761 for the second; Gemini's times in the second ran 1.6 times fast from the video start.
        fake = Fake(reply([(7.4, 'start')], audio=None, video=52801),
                    reply([(1000.75, 'remembering'), (1456.76, 'thank you')], audio=None, video=26761))
        result = run(fake)
        self.assertEqual((len(fake.requests), result['end_detected_by']), (2, 'clip_shorter_than_first'))
        clips = result['raw_response']['clips']
        self.assertEqual([c['duration_seconds'] for c in clips], [600, 304.1])  # 600 x 26761 / 52801 = 304.0965
        self.assertEqual(result['covered_seconds'], [0, 904.1])
        # Scaled by 904.0965 / 1456.76 from the video start: 1000.75 -> 621.087, 1456.76 -> 904.0965.
        self.assertEqual([s['start'] for s in result['segments']], [7.4, 621.09, 904.1])
        self.assertEqual([(c['times'], c['time_scale']) for c in clips], [('clip', None), ('full video', 0.6206)])

    def test_without_any_token_count_an_empty_clip_requires_the_400_eof(self):
        bare = dict(audio=None, video=None)
        fake = Fake(reply([(1, 'a')], **bare), reply([(601, 'b')], **bare), reply([], **bare))
        result = run(fake)
        self.assertEqual(len(fake.requests), 4)
        self.assertEqual((result['clips'], result['end_detected_by'], result['covered_seconds']), (3, 'http_400', [0, 1800]))
        self.assertEqual([s['text'] for s in result['segments']], ['a', 'b'])
        self.assertIsNone(result['raw_response']['clips'][0]['media_tokens'])

    def test_measured_zero_media_and_no_speech_stops_without_a_clip_cap(self):
        fake = Fake(reply([], audio=0, video=0), http_error(401, 'must not be called'))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake)
        self.assertEqual((found.exception.code, found.exception.end_detected_by, len(fake.requests)),
                         ('no_transcript', 'no_speech_no_media', 1))

        later = Fake(reply([(1, 'spoken')]), reply([], audio=0, video=0),
                     http_error(401, 'must not be called'))
        result = run(later)
        self.assertEqual((result['end_detected_by'], len(later.requests), result['clips']),
                         ('no_speech_no_media', 2, 2))

    def test_no_speech_anywhere_is_no_transcript_and_skips_the_title_lookup(self):
        fake = Fake(reply([]), http_error(400, 'offset beyond video'))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake)
        self.assertEqual(found.exception.code, 'no_transcript')
        self.assertEqual(str(found.exception), 'Gemini heard no speech in this video.')
        self.assertEqual(fake.oembed_requests, [])

    def test_a_long_video_has_no_clip_limit(self):
        # 130 full clips (21 h 40 min) and a short last one: only end detection stops the run.
        fake = Fake(*[reply([(i * 600 + 1, f'x{i}')]) for i in range(130)], reply([(130 * 600 + 1, 'end')], audio=SHORT))
        result = run(fake)
        self.assertEqual((len(fake.requests), result['clips'], result['end_detected_by']), (131, 131, 'clip_shorter_than_first'))
        self.assertEqual(result['segments'][-1]['text'], 'end')


class TimestampTests(unittest.TestCase):
    def test_clip_relative_and_full_video_times_are_both_accepted(self):
        fake = Fake(reply([(1, 'a')]), reply([(5, 'x'), (20, 'y'), (590, 'z')]), reply([(1201, 'w')], audio=SHORT))
        result = run(fake)
        self.assertEqual([s['start'] for s in result['segments']], [1.0, 605.0, 620.0, 1190.0, 1201.0])
        clips = result['raw_response']['clips']
        self.assertEqual([c['times'] for c in clips], ['clip', 'clip', 'full video'])
        self.assertEqual(clips[1]['answer_json']['segments'][0]['start'], 5)  # the model's own answer is kept as given

    def test_times_past_the_clip_end_are_scaled_and_no_text_is_dropped(self):
        fake = Fake(reply([(3, 'a'), (603, 'edge-high'), (700, 'too-late')]),
                    reply([(597, 'edge-low'), (1203, 'edge-end'), (1300, 'late'), (900, 'mid')]),
                    reply([], audio=SHORT))
        result = run(fake)
        # Clip 1 (clip times): scaled by 600/700. Clip 2 (full-video times): scaled by 1200/1300, so
        # 597 -> 551.1 is clamped up to the clip start and 900 -> 830.8 may not go back before 1200.
        self.assertEqual([(s['start'], s['text']) for s in result['segments']],
                         [(2.57, 'a'), (516.86, 'edge-high'), (600.0, 'too-late'), (600.0, 'edge-low'),
                          (1110.46, 'edge-end'), (1200.0, 'late'), (1200.0, 'mid')])
        # The last clip is 600 x 44600 / 54600 = 490.1 s long, so the last segment runs to 1690.1.
        self.assertEqual([s['duration'] for s in result['segments']], [514.29, 83.14, 0.0, 510.46, 89.54, 0.0, 490.1])
        self.assertEqual([(c['times'], c['time_scale']) for c in result['raw_response']['clips']],
                         [('clip', 0.8571), ('full video', 0.9231), (None, None)])

    def test_empty_texts_are_dropped_and_the_model_order_is_kept(self):
        fake = Fake(reply([(40, ' later '), (10, '   '), (20, 'earlier')], audio=SHORT))
        result = run(fake)
        self.assertEqual([(s['start'], s['text']) for s in result['segments']], [(40.0, 'later'), (40.0, 'earlier')])


class ResponseHandlingTests(unittest.TestCase):
    def test_invalid_json_and_wrong_shapes_fail_instead_of_dropping_the_clip(self):
        bad = ['not json', '[]', '{"language": "en"}', '{"language": "en", "segments": "none"}',
               '{"segments": [["a", 1]]}', '{"segments": [{"start": 1, "text": 5}]}',
               '{"segments": [{"start": -1, "text": "x"}]}', '{"segments": [{"start": "5", "text": "x"}]}',
               '{"segments": [{"start": true, "text": "x"}]}', '{"segments": [{"start": NaN, "text": "x"}]}',
               '{"segments": [{"start": 1e999, "text": "x"}]}', '{"segments": [{"text": "x"}]}']
        for text in bad:
            fake = Fake(completed(text), reply([(1, 'never')]))
            with self.subTest(text=text), self.assertRaises(providers.ProviderError) as found:
                run(fake)
            self.assertEqual(found.exception.code, 'invalid_response')
            self.assertEqual(len(fake.requests), 1)

    def test_answer_is_the_text_of_the_last_model_output_step(self):
        answer = json.dumps({'language': 'de', 'segments': [{'start': 4, 'text': 'Hallo Welt'}]})
        half = len(answer) // 2
        payload = {'status': 'completed', 'usage': usage(SHORT), 'steps': [
            {'type': 'model_output', 'content': [{'type': 'text', 'text': 'an earlier draft, not JSON'}]},
            {'type': 'model_output', 'content': [{'type': 'text', 'text': answer[:half]}, {'type': 'image'},
                                                 {'type': 'text', 'text': answer[half:]}]}]}
        result = run(Fake(Response(payload)))
        self.assertEqual((result['language'], result['text']), ('de', 'Hallo Welt'))

    def test_unfinished_or_empty_answers_are_errors(self):
        cases = [(completed('{}', status='incomplete'), 'truncated'), (completed('{}', status='failed'), 'service_error'),
                 (Response({'status': 'completed', 'steps': [{'type': 'thought'}]}), 'invalid_response'),
                 (Response({'status': 'completed', 'steps': [{'type': 'model_output', 'content': []}]}), 'invalid_response'),
                 (Response([]), 'invalid_response'), (Response(b'<html>nope</html>'), 'invalid_response'),
                 (Response({}, status=202), 'service_error')]
        for item, code in cases:
            with self.subTest(code=code), self.assertRaises(providers.ProviderError) as found:
                run(Fake(item))
            self.assertEqual(found.exception.code, code)

    def test_max_token_finish_reason_is_rejected_even_with_completed_status_and_json(self):
        answer = json.dumps({'language': 'en', 'segments': [{'start': 1, 'text': 'partial'}],
                             'video_ends_in_this_clip': False})
        payload = {'status': 'completed', 'usage': usage(FULL), 'steps': [
            {'type': 'model_output', 'finishReason': 'MAX_TOKENS', 'content': [{'type': 'text', 'text': answer}]}]}
        with self.assertRaises(providers.ProviderError) as found:
            run(Fake(Response(payload)))
        self.assertEqual(found.exception.code, 'truncated')
        self.assertIn('no partial transcript was saved', str(found.exception))

    def test_oversized_response_is_refused(self):
        with self.assertRaises(providers.ProviderError) as found:
            run(Fake(Response(b'x' * 8_000_001)))
        self.assertEqual(found.exception.code, 'too_large')

    def test_http_errors_map_to_actionable_codes(self):
        # 400-404 fail at once; 500 and 503 are transient, so they fail only after every retry.
        for status, code, attempts in [(400, 'invalid_request', 1), (401, 'authentication', 1), (403, 'authentication', 1),
                                       (404, 'no_transcript', 1), (500, 'service_error', 5), (503, 'service_error', 5)]:
            fake = Fake(*[http_error(status, 'Google says no') for _ in range(attempts)])
            with self.subTest(status=status), self.assertRaises(providers.ProviderError) as found:
                run(fake)
            self.assertEqual((found.exception.code, found.exception.status), (code, status))
            self.assertEqual(len(fake.requests), attempts)
            if status in (401, 403):
                self.assertIn('rejected the API key or the project lacks access', str(found.exception))
            if status in (400, 404, 500):
                self.assertIn('Google says no', str(found.exception))

    def test_unknown_or_unsupported_model_404_is_not_reported_as_a_private_video(self):
        fake = Fake(http_error(404, 'models/gemini-image-only is not found or does not support interactions'))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake)
        self.assertEqual(found.exception.code, 'invalid_model')
        self.assertIn('selected Gemini model', str(found.exception))
        self.assertNotIn('private', str(found.exception))

    def test_connection_problems_are_reported_as_connection(self):
        for problem in [urllib.error.URLError('no route'), TimeoutError(), ConnectionResetError(), http.client.IncompleteRead(b'')]:
            with self.subTest(problem=type(problem).__name__), self.assertRaises(providers.ProviderError) as found:
                run(Fake(*[problem] * 5))
            self.assertEqual(found.exception.code, 'connection')

    def test_key_never_appears_in_error_text_or_messages(self):
        other = 'AIzaSyAnotherKeyShapedStringThatIsNotOurs12345'
        leaks = [http_error(400, f'API key {KEY} is not valid ({other})'), http_error(500, f'upstream {KEY}'),
                 urllib.error.URLError(f'could not connect with {KEY}'), http_error(429, f'quota for {KEY} {other}')]
        for problem in leaks:
            progress = []
            with self.subTest(problem=str(problem)[:30]), self.assertRaises(providers.ProviderError) as found:
                run(Fake(*[problem] * 5), progress=progress.append)
            text = ' '.join([str(found.exception), *progress])
            self.assertNotIn(KEY, text)
            self.assertNotIn(other, text)
        # A 400 that ends the transcript is recorded as a reason; that text is scrubbed too.
        result = run(Fake(reply([(1, 'a')]), http_error(400, f'offset beyond video; key {KEY} {other}')))
        self.assertEqual(result['end_detected_by'], 'http_400')
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn(other, json.dumps(result))


class RetryTests(unittest.TestCase):
    def test_unspecified_quota_429_switches_immediately_and_keeps_failed_clip_provenance(self):
        message = 'Your project has exceeded a quota. See https://ai.dev/rate-limit to manage your rate limits.'
        fake = Fake(reply([(1, 'first')]), http_error(429, message),
                    reply([(601, 'second')], audio=SHORT))
        deferred, sleeper, progress = set(), Sleeper(), []
        result = run(fake, model='gemini-a', fallback_models=['gemini-b'],
                     quota_unknown_models=deferred, sleep=sleeper, progress=progress.append)
        self.assertEqual([(body['model'], body['input'][0]['processing']['start_offset']) for body in fake.bodies()],
                         [('gemini-a', '0s'), ('gemini-a', '600s'),
                          ('gemini-b', '600s'), ('gemini-b', '1200s')])
        self.assertEqual((result['text'].splitlines(), result['requests'], sleeper.calls),
                         (['first', 'second'], 4, []))
        self.assertEqual(result['end_detected_by'], 'http_400')
        self.assertEqual(deferred, {'gemini-a'})
        failure = result['quota_unknown_failures'][0]
        self.assertEqual({key: failure[key] for key in ('model', 'reason', 'next_model', 'clip_start',
                                                        'clip_end', 'http_status', 'attempts', 'request_number')},
                         {'model': 'gemini-a', 'reason': 'unspecified_quota', 'next_model': 'gemini-b',
                          'clip_start': 600, 'clip_end': 1200, 'http_status': 429, 'attempts': 1,
                          'request_number': 2})
        self.assertIn('quota type unspecified', ' '.join(progress).lower())

    def test_unspecified_quota_gets_one_fresh_preferred_model_try_on_each_video(self):
        message = 'Your project has exceeded a quota. See https://ai.dev/rate-limit to manage your rate limits.'
        deferred, known = set(), lambda vid, cancel: 600
        first = Fake(http_error(429, message), reply([(1, 'first')], ended=True))
        run(first, model='gemini-a', fallback_models=['gemini-b'], quota_unknown_models=deferred,
            duration_reader=known)
        second = Fake(reply([(1, 'second')], ended=True))
        run(second, model='gemini-a', fallback_models=['gemini-b'], quota_unknown_models=deferred,
            duration_reader=known)
        unrelated = Fake(reply([(1, 'third')], ended=True))
        run(unrelated, model='gemini-a', fallback_models=['gemini-b'], quota_unknown_models=set(),
            duration_reader=known)
        self.assertEqual([body['model'] for body in second.bodies()], ['gemini-a'])
        self.assertEqual([body['model'] for body in unrelated.bodies()], ['gemini-a'])
        self.assertEqual(deferred, set())

    def test_all_unspecified_and_mixed_unavailable_messages_do_not_invent_a_reset(self):
        unknown = 'Your project has exceeded a quota. See https://ai.dev/rate-limit to manage your rate limits.'
        fake = Fake(http_error(429, unknown), http_error(429, unknown))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake, model='gemini-a', fallback_models=['gemini-b'])
        self.assertEqual((found.exception.code, found.exception.status, found.exception.requests),
                         ('unspecified_quota', 429, 2))
        self.assertIn('No reset time is known', str(found.exception))
        self.assertNotIn('daily quota', str(found.exception).lower())
        self.assertNotIn('midnight', str(found.exception).lower())
        self.assertEqual(len(found.exception.quota_unknown_failures), 2)

        daily = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                  'violations': [{'quotaId': 'RequestsPerDay'}]}]
        mixed = Fake(http_error(429, unknown), http_error(503, HIGH_DEMAND),
                     http_error(429, 'daily', details=daily))
        with self.assertRaises(providers.ProviderError) as found:
            run(mixed, model='gemini-a', fallback_models=['gemini-b', 'gemini-c'],
                wall_clock=lambda: datetime(2026, 10, 15, 16, tzinfo=timezone.utc))
        text = str(found.exception)
        self.assertEqual((found.exception.code, found.exception.requests), ('models_unavailable', 3))
        self.assertIn('quota exceeded (type and reset unknown): gemini-a', text)
        self.assertIn('temporarily busy (high demand): gemini-b', text)
        self.assertIn('daily quota exhausted: gemini-c', text)
        self.assertIn('For those daily-limited models', text)
        self.assertNotIn('All configured Gemini models reached their daily quota', text)

    def test_explicit_high_demand_503_switches_immediately_and_retries_only_failed_clip(self):
        fake = Fake(reply([(1, 'first')]), http_error(503, HIGH_DEMAND),
                    reply([(601, 'second')], audio=SHORT))
        sleeper, progress, busy = Sleeper(), [], set()
        result = run(fake, model='gemini-a', fallback_models=['gemini-b'], busy_models=busy,
                     sleep=sleeper, progress=progress.append)
        self.assertEqual([(body['model'], body['input'][0]['processing']['start_offset']) for body in fake.bodies()],
                         [('gemini-a', '0s'), ('gemini-a', '600s'),
                          ('gemini-b', '600s'), ('gemini-b', '1200s')])
        self.assertEqual(result['text'].splitlines(), ['first', 'second'])
        self.assertEqual([segment['start'] for segment in result['segments']], [1.0, 601.0])
        self.assertEqual([clip['model'] for clip in result['raw_response']['clips']], ['gemini-a', 'gemini-b'])
        self.assertEqual(result['raw_response']['availability_failures'], [{
            'model': 'gemini-a', 'clip_start': 600, 'message': mock.ANY, 'http_status': 503, 'attempts': 1,
            'next_model': 'gemini-b'}])
        self.assertEqual(result['availability_failures'], result['raw_response']['availability_failures'])
        self.assertEqual((result['requests'], result['request_counts']['gemini_api'], sleeper.calls), (4, 4, []))
        self.assertEqual(result['end_detected_by'], 'http_400')
        self.assertEqual(busy, {'gemini-a'})
        self.assertIn('Gemini · gemini-a temporarily busy (high demand) for 10:00-20:00; '
                      'retrying this clip with gemini-b', progress)

    def test_capacity_gets_one_fresh_preferred_model_try_on_each_video(self):
        busy = set()
        known = lambda vid, cancel: 600
        first = Fake(http_error(503, HIGH_DEMAND), reply([(1, 'first')], ended=True))
        run(first, model='gemini-a', fallback_models=['gemini-b'], busy_models=busy, duration_reader=known)
        second = Fake(reply([(1, 'second')], ended=True))
        run(second, model='gemini-a', fallback_models=['gemini-b'], busy_models=busy, duration_reader=known)
        unrelated = Fake(reply([(1, 'third')], ended=True))
        run(unrelated, model='gemini-a', fallback_models=['gemini-b'], busy_models=set(), duration_reader=known)
        self.assertEqual([body['model'] for body in second.bodies()], ['gemini-a'])
        self.assertEqual([body['model'] for body in unrelated.bodies()], ['gemini-a'])
        self.assertEqual(busy, set())

    def test_all_capacity_busy_is_temporary_while_all_daily_names_only_daily_reset(self):
        busy = Fake(http_error(503, HIGH_DEMAND), http_error(503, 'model at capacity'))
        with self.assertRaises(providers.ProviderError) as busy_found:
            run(busy, model='gemini-a', fallback_models=['gemini-b'])
        self.assertEqual((busy_found.exception.code, busy_found.exception.status,
                          busy_found.exception.requests), ('capacity', 503, 2))
        self.assertIn('temporarily busy', str(busy_found.exception))
        self.assertNotIn('midnight', str(busy_found.exception))
        self.assertEqual(len(busy_found.exception.availability_failures), 2)
        self.assertEqual(busy_found.exception.quota_failures, [])

        quota = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                  'violations': [{'quotaId': 'RequestsPerDay'}]}]
        daily = Fake(http_error(429, 'daily', details=quota), http_error(429, 'daily', details=quota))
        with self.assertRaises(providers.ProviderError) as daily_found:
            run(daily, model='gemini-a', fallback_models=['gemini-b'],
                wall_clock=lambda: datetime(2026, 10, 15, 16, tzinfo=timezone.utc))
        self.assertEqual((daily_found.exception.code, daily_found.exception.status,
                          daily_found.exception.requests), ('daily_quota', 429, 2))
        self.assertIn('midnight America/Los_Angeles', str(daily_found.exception))
        self.assertEqual(daily_found.exception.availability_failures, [])
        self.assertEqual(len(daily_found.exception.quota_failures), 2)

    def test_capacity_and_daily_terminal_message_reports_both_without_capacity_reset_claim(self):
        quota = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                  'violations': [{'quotaId': 'RequestsPerDay'}]}]
        fake = Fake(http_error(503, HIGH_DEMAND), http_error(429, 'daily', details=quota))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake, model='gemini-a', fallback_models=['gemini-b'],
                wall_clock=lambda: datetime(2026, 10, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(found.exception.code, 'capacity_and_daily_quota')
        self.assertIn('temporarily busy (high demand): gemini-a', str(found.exception))
        self.assertIn('daily quota exhausted: gemini-b', str(found.exception))
        self.assertIn("Google's daily quota resets", str(found.exception))

    def test_429_waits_the_retry_after_seconds_then_succeeds(self):
        fake, sleeper, progress = Fake(http_error(429, headers={'Retry-After': '7'}), reply([(1, 'a')], audio=SHORT)), Sleeper(), []
        result = run(fake, sleep=sleeper, progress=progress.append)
        self.assertEqual(sleeper.calls, [7.0])
        self.assertEqual(len(fake.requests), 3)  # the retry, then the next clip that Google answers with 400
        self.assertEqual(fake.bodies()[0]['input'][0]['processing'], fake.bodies()[1]['input'][0]['processing'])
        self.assertEqual(result['raw_response']['clips'][0]['retries'], 1)
        self.assertIn('Gemini · rate limited (HTTP 429), waiting 7s before retry 1 of 4', progress)

    def test_backoff_without_a_usable_header_then_gives_up_after_four_retries(self):
        # Google's documented SDK default: about 1 s, doubling, at most 60 s, plus jitter (here 0).
        sleeper = Sleeper()
        fake = Fake(*[http_error(429, 'Quota exceeded for requests per minute') for _ in range(5)], reply([(1, 'never')]))
        with mock.patch.object(gemini.random, 'random', return_value=0.0), self.assertRaises(providers.ProviderError) as found:
            run(fake, sleep=sleeper)
        self.assertEqual((found.exception.code, found.exception.status), ('rate_limit', 429))
        self.assertIn('Quota exceeded for requests per minute', str(found.exception))
        self.assertIn('after 4 retries', str(found.exception))
        self.assertEqual(sleeper.calls, [1, 2, 4, 8])
        self.assertEqual(len(fake.requests), 5)
        with mock.patch.object(gemini.random, 'random', return_value=0.5):
            self.assertEqual([gemini.backoff(n) for n in (1, 6, 7, 10)], [1.5, 32.5, 60.5, 60.5])
        for header, wait in [('soon', 1), ('-5', 1), ('60', 60.0)]:
            sleeper = Sleeper()
            with self.subTest(header=header), mock.patch.object(gemini.random, 'random', return_value=0.0):
                run(Fake(http_error(429, headers={'Retry-After': header}), reply([(1, 'a')], audio=SHORT)), sleep=sleeper)
                self.assertEqual(sleeper.calls, [wait])

    def test_a_wait_longer_than_60_seconds_fails_at_once(self):
        sleeper, fake = Sleeper(), Fake(http_error(429, headers={'Retry-After': '99999'}), reply([(1, 'never')]))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake, sleep=sleeper)
        self.assertEqual(found.exception.code, 'rate_limit')
        self.assertIn('Not retried: Google asked for a wait of 1667 minutes.', str(found.exception))
        self.assertEqual((sleeper.calls, len(fake.requests)), ([], 1))

    def test_the_free_tier_daily_limit_fails_at_once_with_googles_message(self):
        # Google's live answer on 2026-10-14 once the day's free-tier requests were used up.
        message = ('Rate limit exceeded for model gemini-3.8-flash (limit: 20 requests per day on Free Tier). '
                   'Please retry in 1h59m7s or upgrade your tier at https://ai.dev/rate-limit.')
        sleeper, fake = Sleeper(), Fake(http_error(429, message, headers={'Retry-After': '7147'}), reply([(1, 'never')]))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake, sleep=sleeper)
        self.assertEqual((found.exception.code, found.exception.status), ('daily_quota', 429))
        self.assertIn('daily quota resets at midnight America/Los_Angeles', str(found.exception))
        self.assertEqual((sleeper.calls, len(fake.requests)), ([], 1))

    def test_structured_daily_quota_switches_the_failed_clip_once_and_keeps_provenance(self):
        quota = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                  'violations': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]},
                 {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '43200s'}]
        fake = Fake(reply([(1, 'first')]), http_error(429, 'quota', details=quota),
                    reply([(601, 'second')], audio=SHORT))
        exhausted = {}
        result = run(fake, model='gemini-a', fallback_models=['gemini-b'], exhausted_models=exhausted,
                     wall_clock=lambda: datetime(2026, 10, 15, 16, tzinfo=timezone.utc))
        self.assertEqual([(b['model'], b['input'][0]['processing']['start_offset']) for b in fake.bodies()],
                         [('gemini-a', '0s'), ('gemini-a', '600s'),
                          ('gemini-b', '600s'), ('gemini-b', '1200s')])
        self.assertEqual(result['models'], ['gemini-a', 'gemini-b'])
        self.assertEqual([clip['model'] for clip in result['raw_response']['clips']], ['gemini-a', 'gemini-b'])
        self.assertEqual(result['raw_response']['quota_failures'][0]['model'], 'gemini-a')
        self.assertEqual((result['requests'], result['end_detected_by']), (4, 'http_400'))
        self.assertIn('gemini-a', exhausted)

    def test_transient_429_does_not_switch_models(self):
        details = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                    'violations': [{'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel'}]},
                   {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '2s'}]
        fake = Fake(http_error(429, 'RPM exhausted', details=details), reply([(1, 'done')], ended=True))
        deferred = set()
        result = run(fake, model='gemini-a', fallback_models=['gemini-b'],
                     quota_unknown_models=deferred, duration_reader=lambda vid, cancel: 600)
        self.assertEqual([body['model'] for body in fake.bodies()], ['gemini-a', 'gemini-a'])
        self.assertEqual(result['models'], ['gemini-a'])
        self.assertEqual(deferred, set())

    def test_exhaustion_carries_to_the_next_video_in_only_the_same_batch(self):
        quota = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                  'violations': [{'quotaId': 'RequestsPerDay'}]}]
        wall = lambda: datetime(2026, 10, 15, 16, tzinfo=timezone.utc)
        exhausted = {}
        first = Fake(http_error(429, 'daily quota', details=quota), reply([(1, 'first')], ended=True))
        known = lambda vid, cancel: 600
        run(first, model='gemini-a', fallback_models=['gemini-b'], exhausted_models=exhausted,
            wall_clock=wall, duration_reader=known)
        second = Fake(reply([(1, 'second')], ended=True))
        run(second, model='gemini-a', fallback_models=['gemini-b'], exhausted_models=exhausted,
            wall_clock=wall, duration_reader=known)
        unrelated = Fake(reply([(1, 'third')], ended=True))
        run(unrelated, model='gemini-a', fallback_models=['gemini-b'], exhausted_models={},
            wall_clock=wall, duration_reader=known)
        self.assertEqual([body['model'] for body in second.bodies()], ['gemini-b'])
        self.assertEqual([body['model'] for body in unrelated.bodies()], ['gemini-a'])

    def test_all_daily_exhausted_names_pacific_midnight_and_eastern_time(self):
        quota = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                  'violations': [{'quotaId': 'RequestsPerDay'}]}]
        fake = Fake(http_error(429, 'daily', details=quota), http_error(429, 'daily', details=quota))
        with self.assertRaises(providers.ProviderError) as found:
            run(fake, model='gemini-a', fallback_models=['gemini-b'],
                wall_clock=lambda: datetime(2026, 10, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(found.exception.code, 'daily_quota')
        self.assertIn('2026-10-16 12:00 AM PDT', str(found.exception))
        self.assertIn('2026-10-16 03:00 AM EDT', str(found.exception))
        self.assertEqual([body['model'] for body in fake.bodies()], ['gemini-a', 'gemini-b'])

    def test_daily_quota_uses_windows_timezone_rules_when_iana_data_is_missing(self):
        quota = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                  'violations': [{'quotaId': 'RequestsPerDay'}]}]
        cases = (
            ('winter', datetime(2026, 1, 15, 16, tzinfo=timezone.utc),
             datetime(2026, 1, 16, 8, tzinfo=timezone.utc), '2026-01-16 12:00 AM PST',
             '2026-01-16 03:00 AM EST'),
            ('summer', datetime(2026, 7, 15, 16, tzinfo=timezone.utc),
             datetime(2026, 7, 16, 7, tzinfo=timezone.utc), '2026-07-16 12:00 AM PDT',
             '2026-07-16 03:00 AM EDT'),
            ('spring midnight before transition', datetime(2026, 3, 7, 12, tzinfo=timezone.utc),
             datetime(2026, 3, 8, 8, tzinfo=timezone.utc), '2026-03-08 12:00 AM PST',
             '2026-03-08 04:00 AM EDT'),
            ('spring midnight after transition', datetime(2026, 3, 8, 12, tzinfo=timezone.utc),
             datetime(2026, 3, 9, 7, tzinfo=timezone.utc), '2026-03-09 12:00 AM PDT',
             '2026-03-09 03:00 AM EDT'),
            ('fall midnight before transition', datetime(2026, 10, 31, 12, tzinfo=timezone.utc),
             datetime(2026, 11, 1, 7, tzinfo=timezone.utc), '2026-11-01 12:00 AM PDT',
             '2026-11-01 02:00 AM EST'),
            ('fall midnight after transition', datetime(2026, 11, 1, 12, tzinfo=timezone.utc),
             datetime(2026, 11, 2, 8, tzinfo=timezone.utc), '2026-11-02 12:00 AM PST',
             '2026-11-02 03:00 AM EST'),
        )
        for label, moment, expected_reset, pacific_text, eastern_text in cases:
            fake = Fake(http_error(429, 'daily', details=quota), http_error(429, 'daily', details=quota))
            exhausted = {}
            with self.subTest(season=label):
                with (mock.patch.object(gemini, 'ZoneInfo', side_effect=ZoneInfoNotFoundError('simulated missing tzdata')),
                      self.assertRaises(providers.ProviderError) as found):
                    run(fake, model='gemini-a', fallback_models=['gemini-b'], exhausted_models=exhausted,
                        wall_clock=lambda moment=moment: moment)
                self.assertEqual(found.exception.code, 'daily_quota')
                self.assertEqual(set(exhausted.values()), {expected_reset})
                self.assertIn(pacific_text, str(found.exception))
                self.assertIn(eastern_text, str(found.exception))

    def test_daily_quota_keeps_the_normal_iana_timezone_path_when_available(self):
        zones = {
            'America/Los_Angeles': timezone(timedelta(hours=-7), 'PDT'),
            'America/New_York': timezone(timedelta(hours=-4), 'EDT'),
        }
        quota = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                  'violations': [{'quotaId': 'RequestsPerDay'}]}]
        fake = Fake(http_error(429, 'daily', details=quota), http_error(429, 'daily', details=quota))
        with (mock.patch.object(gemini, 'ZoneInfo', side_effect=zones.__getitem__),
              mock.patch.object(gemini, '_windows_local_datetime', side_effect=AssertionError('fallback used')),
              mock.patch.object(gemini, '_windows_local_midnight_utc', side_effect=AssertionError('fallback used')),
              self.assertRaises(providers.ProviderError) as found):
            run(fake, model='gemini-a', fallback_models=['gemini-b'],
                wall_clock=lambda: datetime(2026, 7, 15, 16, tzinfo=timezone.utc))
        self.assertEqual(found.exception.code, 'daily_quota')
        self.assertIn('2026-07-16 12:00 AM PDT', str(found.exception))
        self.assertIn('2026-07-16 03:00 AM EDT', str(found.exception))

    def test_transient_server_and_network_errors_are_retried(self):
        progress = []
        fake = Fake(http_error(503), http_error(408), urllib.error.URLError('reset'), reply([(1, 'a')], audio=SHORT))
        result = run(fake, progress=progress.append)
        clip = result['raw_response']['clips'][0]
        self.assertEqual((len(fake.requests), clip['retries']), (5, 3))  # 4 for the first clip, then the 400 that ends it
        self.assertEqual([e['status'] for e in clip['retry_errors']], [503, 408, None])
        self.assertEqual(sum('temporary error' in line for line in progress), 3)
        self.assertTrue(any('temporary error (HTTP 503)' in line for line in progress))

    def test_client_errors_are_not_retried(self):
        for status in (400, 401, 403, 404):
            fake = Fake(http_error(status), reply([(1, 'never')]))
            with self.subTest(status=status), self.assertRaises(providers.ProviderError):
                run(fake)
            self.assertEqual(len(fake.requests), 1)

    def test_a_rate_limit_on_a_later_clip_retries_that_clip_only(self):
        fake = Fake(reply([(1, 'a')]), http_error(429), reply([(601, 'b')], audio=SHORT))
        result = run(fake)
        self.assertEqual([r['input'][0]['processing']['start_offset'] for r in fake.bodies()], ['0s', '600s', '600s'])
        self.assertEqual(result['clips'], 2)


class CancelTests(unittest.TestCase):
    def test_cancel_before_start_makes_no_request(self):
        cancel = threading.Event()
        cancel.set()
        fake = Fake(reply([(1, 'a')]))
        with self.assertRaises(InterruptedError):
            run(fake, cancel=cancel)
        self.assertEqual(fake.requests, [])

    def test_cancel_between_clips_stops_before_the_next_clip(self):
        cancel = threading.Event()
        def first():
            cancel.set()
            return reply([(1, 'a')])
        fake = Fake(first, reply([(601, 'b')]))
        with self.assertRaises(InterruptedError):
            run(fake, cancel=cancel)
        self.assertEqual(len(fake.requests), 1)

    def test_cancel_during_a_retry_wait_stops_the_retry(self):
        cancel = threading.Event()
        fake = Fake(http_error(429), reply([(1, 'a')]))
        with self.assertRaises(InterruptedError):
            run(fake, cancel=cancel, sleep=Sleeper(hook=cancel.set))
        self.assertEqual(len(fake.requests), 1)

    def test_cancel_after_daily_limit_does_not_switch_model(self):
        cancel = threading.Event()
        quota = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                  'violations': [{'quotaId': 'RequestsPerDay'}]}]
        def limited():
            cancel.set()
            return http_error(429, 'daily', details=quota)
        fake = Fake(limited, reply([(1, 'never')]))
        with self.assertRaises(InterruptedError):
            run(fake, model='gemini-a', fallback_models=['gemini-b'], cancel=cancel)
        self.assertEqual(len(fake.requests), 1)

    def test_cancel_after_capacity_failure_does_not_switch_model(self):
        cancel = threading.Event()
        def busy():
            cancel.set()
            return http_error(503, HIGH_DEMAND)
        fake = Fake(busy, reply([(1, 'never')]))
        with self.assertRaises(InterruptedError):
            run(fake, model='gemini-a', fallback_models=['gemini-b'], cancel=cancel)
        self.assertEqual(len(fake.requests), 1)

    def test_cancel_after_unspecified_quota_does_not_switch_model(self):
        cancel = threading.Event()
        def limited():
            cancel.set()
            return http_error(429, 'Your project has exceeded a quota. See https://ai.dev/rate-limit to manage your rate limits.')
        fake = Fake(limited, reply([(1, 'never')]))
        with self.assertRaises(InterruptedError):
            run(fake, model='gemini-a', fallback_models=['gemini-b'], cancel=cancel)
        self.assertEqual(len(fake.requests), 1)

    def test_real_wait_is_interrupted_by_cancel(self):
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(InterruptedError):
                gemini.fetch(VID, KEY, opener=Fake(http_error(429, headers={'Retry-After': '60'}), reply([(1, 'a')])),
                             cancel=cancel, duration_reader=lambda vid, event: None)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 10)


class ResultTests(unittest.TestCase):
    def test_title_comes_from_oembed_and_its_failure_is_tolerated(self):
        fake = Fake(reply([(1, 'a')], audio=SHORT))
        self.assertEqual(run(fake)['title'], 'Fixture title')
        request, timeout = fake.oembed_requests[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(query, {'url': [f'https://www.youtube.com/watch?v={VID}'], 'format': ['json']})
        self.assertEqual(timeout, 15)
        self.assertIsNone(request.get_header('X-goog-api-key'))
        self.assertNotIn(KEY, request.full_url)
        for broken in [urllib.error.URLError('offline'), TimeoutError(), http_error(404), RuntimeError('anything'),
                       Response(b'not json'), Response({'no_title': 1}), Response({'title': '   '}), Response([1])]:
            with self.subTest(broken=repr(broken)[:40]):
                result = run(Fake(reply([(1, 'a')], audio=SHORT), oembed=broken))
                self.assertEqual(result['title'], VID)
                self.assertEqual(result['segments'][0]['text'], 'a')

    def test_result_has_the_shape_and_hash_of_the_other_services(self):
        result = run(Fake(reply([(1.5, 'There are twelve garden beds.')], language='en', audio=SHORT)))
        other = providers.normalize('fetchtranscript', {
            'video_id': VID, 'language': 'en',
            'segments': [{'text': 'There are twelve garden beds.', 'start': 1.5, 'duration': 598.5}]}, VID)
        self.assertEqual(set(other) - set(result), set())
        self.assertEqual(result['hash'], other['hash'])
        self.assertEqual((result['provider'], result['video_id'], result['language']), ('gemini', VID, 'en'))
        self.assertIsNone(result['generated_captions'])
        self.assertIsNone(result['credits_remaining'])
        self.assertIsNone(result['billable_credits'])
        self.assertIsInstance(result['elapsed_seconds'], float)
        json.dumps(result)  # the whole result, raw responses included, must be storable as JSON

    def test_elapsed_time_uses_the_injected_clock(self):
        ticks = iter([100.0, 142.5])
        result = run(Fake(reply([(1, 'a')], audio=SHORT)), clock=lambda: next(ticks))
        self.assertEqual(result['elapsed_seconds'], 42.5)

    def test_language_comes_from_the_first_clip_that_has_speech(self):
        result = run(Fake(reply([], language='xx'), reply([(601, 'a')], language='fr'), reply([(1201, 'b')], language='en', audio=SHORT)))
        self.assertEqual(result['language'], 'fr')

    def test_progress_reports_each_clip(self):
        progress = []
        run(Fake(reply([(1, 'a')]), reply([(601, 'b')], audio=SHORT)), progress=progress.append)
        self.assertEqual(progress, ['Gemini · exact YouTube duration unavailable; using clip evidence to detect the end.',
                                    'Gemini · gemini-3.8-flash · transcribing 00:00-10:00 (part 1)',
                                    'Gemini · gemini-3.8-flash · transcribing 10:00-20:00 (part 2)'])
        self.assertEqual(gemini.clip_label(3600), '1:00:00')

    def test_progress_names_the_part_count_when_the_duration_is_known(self):
        progress = []
        run(Fake(reply([(1, 'one')]), reply([(601, 'two')]), reply([(1201, 'three')], audio=SHORT)),
            duration_reader=lambda vid, cancel: 1250, progress=progress.append)
        self.assertEqual([line for line in progress if 'transcribing' in line],
                         ['Gemini · gemini-3.8-flash · transcribing 00:00-10:00 (part 1 of 3)',
                          'Gemini · gemini-3.8-flash · transcribing 10:00-20:00 (part 2 of 3)',
                          'Gemini · gemini-3.8-flash · transcribing 20:00-20:50 (part 3 of 3)'])


def model(name, methods=('generateContent',), display=None, limit=1048576):
    entry = {'name': 'models/' + name, 'supportedGenerationMethods': list(methods), 'inputTokenLimit': limit, 'outputTokenLimit': 65536}
    if display is not None:
        entry['displayName'] = display
    return entry


def listing(*models, token=None):
    return Response({'models': list(models), **({'nextPageToken': token} if token else {})})


class ModelListTests(unittest.TestCase):
    """gemini.list_models: Google's models.list, with fixture replies only."""
    def query(self, request):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)

    def test_keeps_only_gemini_models_that_generate_content_sorted_by_id(self):
        fake = Fake(listing(
            model('gemini-3.8-pro', display='Gemini 3.8 Pro', limit=2000000), model('gemini-3.8-flash', display='Gemini 3.8 Flash'),
            model('gemini-2.5-flash'), model('gemini-embedding-001', methods=['embedContent']),
            model('embedding-001', methods=['embedContent']), model('aqa', methods=['generateAnswer']),
            model('gemma-3-27b-it'), model('imagen-4.0-generate', methods=['predict', 'generateContent']),
            model('gemini-with space'), 'models/gemini-text-only', {'name': 'models/gemini-no-methods'}, {'supportedGenerationMethods': ['generateContent']},
            {'name': 7, 'supportedGenerationMethods': ['generateContent']}))
        result = gemini.list_models(KEY, opener=fake)
        self.assertEqual(result, [{'id': 'gemini-2.5-flash', 'label': 'gemini-2.5-flash', 'input_tokens': 1048576, 'output_tokens': 65536},
                                  {'id': 'gemini-3.8-flash', 'label': 'Gemini 3.8 Flash', 'input_tokens': 1048576, 'output_tokens': 65536},
                                  {'id': 'gemini-3.8-pro', 'label': 'Gemini 3.8 Pro', 'input_tokens': 2000000, 'output_tokens': 65536}])

    def test_request_shape_and_key_only_in_header(self):
        fake = Fake(listing(model('gemini-3.8-flash')))
        gemini.list_models(KEY, opener=fake)
        (request, timeout), = fake.requests
        self.assertEqual((request.get_method(), timeout), ('GET', 15))
        self.assertEqual(request.full_url.split('?')[0], 'https://generativelanguage.googleapis.com/v1beta/models')
        self.assertEqual(self.query(request), {'pageSize': ['1000']})
        self.assertEqual(request.get_header('X-goog-api-key'), KEY)
        self.assertEqual(request.get_header('Accept'), 'application/json')
        self.assertNotIn(KEY, request.full_url)
        self.assertIsNone(request.data)
        self.assertEqual(fake.oembed_requests, [])

    def test_follows_next_page_token_and_merges_pages(self):
        fake = Fake(listing(model('gemini-b'), model('gemini-a'), token='page 2/&'),
                    listing(model('gemini-c'), model('gemini-a', display='Again'), token='page-3'),
                    listing(model('gemini-d')))
        result = gemini.list_models(KEY, opener=fake)
        self.assertEqual([self.query(r).get('pageToken') for r, _ in fake.requests], [None, ['page 2/&'], ['page-3']])
        self.assertEqual([self.query(r)['pageSize'] for r, _ in fake.requests], [['1000']] * 3)
        self.assertEqual([m['id'] for m in result], ['gemini-a', 'gemini-b', 'gemini-c', 'gemini-d'])
        self.assertEqual(len(fake.requests), 3)

    def test_every_page_is_read_however_many_there_are(self):
        pages = [listing(model(f'gemini-{n:02}'), token=f't{n}') for n in range(14)] + [listing(model('gemini-14'))]
        result = gemini.list_models(KEY, opener=Fake(*pages))
        self.assertEqual([m['id'] for m in result], [f'gemini-{n:02}' for n in range(15)])

    def test_a_repeated_page_token_fails_instead_of_looping(self):
        fake = Fake(listing(model('gemini-a'), token='t1'), listing(model('gemini-b'), token='t2'), listing(model('gemini-c'), token='t1'))
        with self.assertRaises(providers.ProviderError) as found:
            gemini.list_models(KEY, opener=fake)
        self.assertEqual((found.exception.code, str(found.exception)), ('invalid_response', "Google's model list repeated a page."))
        self.assertEqual(len(fake.requests), 3)

    def test_an_empty_list_is_an_empty_list(self):
        self.assertEqual(gemini.list_models(KEY, opener=Fake(Response({}))), [])
        self.assertEqual(gemini.list_models(KEY, opener=Fake(listing())), [])

    def test_no_key_means_no_request(self):
        fake = Fake()
        with self.assertRaises(providers.ProviderError) as found:
            gemini.list_models('', opener=fake)
        self.assertEqual((found.exception.code, str(found.exception)), ('missing_key', 'Save a Gemini key first.'))
        for bad in ('line\nbreak', 'café'):
            with self.subTest(key=bad), self.assertRaises(providers.ProviderError) as found:
                gemini.list_models(bad, opener=fake)
            self.assertEqual(found.exception.code, 'authentication')
        self.assertEqual(fake.requests, [])

    def test_http_errors_map_like_the_transcript_call_and_never_show_the_key(self):
        for status, code in ((400, 'invalid_request'), (401, 'authentication'), (403, 'authentication'), (429, 'rate_limit'), (500, 'service_error')):
            for key in (KEY, 'plain-secret-key'):
                with self.subTest(status=status, key=key), self.assertRaises(providers.ProviderError) as found:
                    gemini.list_models(key, opener=Fake(http_error(status, f'API key not valid: {key} please retry')))
                self.assertEqual((found.exception.code, found.exception.status), (code, status))
                self.assertNotIn(key, str(found.exception))
        with self.assertRaises(providers.ProviderError) as found:
            gemini.list_models('plain-secret-key', opener=Fake(http_error(400, 'bad plain-secret-key')))
        self.assertEqual(str(found.exception), 'Gemini rejected the request: bad [API key redacted]')

    def test_network_failures_and_bad_replies(self):
        cases = [(urllib.error.URLError(f'refused {KEY}'), 'connection'), (TimeoutError(f'slow {KEY}'), 'connection'),
                 (http.client.IncompleteRead(b'x'), 'connection'),
                 (Response(b'not json'), 'invalid_response'), (Response([1, 2]), 'invalid_response'),
                 (Response({'models': 'nope'}), 'invalid_response'), (Response({'models': []}, status=204), 'service_error'),
                 (Response(b' ' * (gemini.MAX_RESPONSE_BYTES + 1)), 'too_large')]
        for item, code in cases:
            with self.subTest(code=code, item=repr(item)[:40]), self.assertRaises(providers.ProviderError) as found:
                gemini.list_models(KEY, opener=Fake(item))
            self.assertEqual(found.exception.code, code)
            self.assertNotIn(KEY, str(found.exception))

    def test_the_default_opener_is_urllib_and_nothing_else_is_called(self):
        with mock.patch.object(gemini.urllib.request, 'urlopen', return_value=listing(model('gemini-x'))) as urlopen:
            self.assertEqual([m['id'] for m in gemini.list_models(KEY)], ['gemini-x'])
        self.assertEqual(urlopen.call_args.kwargs, {'timeout': 15})
        self.assertEqual(urlopen.call_args.args[0].get_header('X-goog-api-key'), KEY)


if __name__ == '__main__':
    unittest.main()
