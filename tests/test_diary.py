import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from hourly_diary.config import Config
from hourly_diary.core import Diary, parse_range
from hourly_diary.exporting import build_export
from hourly_diary.maintenance import backup, restore, integrity
from hourly_diary.media import MediaWorker, MediaError, HTTPTranscriber
from hourly_diary.store import Store
from hourly_diary.sync import import_bundle, merge_note, atomic_note
from hourly_diary.runtime import process_lock, TelegramTransport

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 6, 0, 10, tzinfo=UTC)  # 09:00 Moscow
OWNER = 10001


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.cfg = Config('dummy-not-a-token', OWNER, self.data)
        self.store = Store(self.cfg.db_path)
        self.diary = Diary(self.store, self.cfg)
        self.uid = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def update(self, text=None, reply=None, voice=False, owner=OWNER, kind='message', when=NOW, chat_type='private', mid=None):
        self.uid += 1
        m = {'message_id': mid or self.uid, 'date': int(when.timestamp()),
             'from': {'id': owner, 'first_name': 'not retained'},
             'chat': {'id': owner, 'type': chat_type}}
        if text is not None:
            m['text'] = text
        if reply:
            m['reply_to_message'] = {'message_id': reply, 'text': 'must not persist'}
        if voice:
            m['voice'] = {'file_id': 'opaque-fixture-id', 'file_unique_id': 'fixture-unique',
                          'file_size': 8, 'duration': 2, 'mime_type': 'audio/ogg'}
        return {'update_id': self.uid, kind: m}

    def apply(self, update):
        self.diary.receive(update)
        self.diary.process(self.diary.sanitize_update(update), NOW)

    def rows(self):
        return self.store.entries('0001-01-01', '9999-12-31')

    def activate(self):
        self.apply(self.update('/start'))

    def prompt(self):
        self.activate()
        prompt = self.diary.schedule(NOW)
        self.diary.sent_prompt(prompt['id'], 900)
        return prompt


class LogicTests(Base):
    def test_whitelist_never_first_user(self):
        self.apply(self.update('/start', owner=999))
        self.assertIsNone(self.store.get_setting('chat_id'))
        self.apply(self.update('not private', chat_type='group'))
        self.assertEqual(self.rows(), [])
        self.assertIsNone(self.diary.schedule(NOW))
        body = self.store.db.execute('SELECT body FROM inbox ORDER BY update_id LIMIT 1').fetchone()[0]
        self.assertNotIn('not retained', body)

    def test_config_owner_chat_and_stt(self):
        with self.assertRaises(ValueError):
            Config('', 0)
        with self.assertRaises(ValueError):
            Config('', OWNER, chat_id=42)
        with self.assertRaises(ValueError):
            Config('', OWNER, stt_endpoint='http://insecure')
        cfg = replace(self.cfg, stt_provider='groq')
        self.assertEqual(cfg.model, 'whisper-large-v3-turbo')
        self.assertEqual(cfg.endpoint, 'https://api.groq.com/openai/v1/audio/transcriptions')
        self.assertNotIn('dummy-not-a-token', repr(cfg))

    def test_known_chat_enables_without_start(self):
        diary = Diary(self.store, replace(self.cfg, chat_id=OWNER))
        self.assertIsNotNone(diary.schedule(NOW))

    def test_schedule_exact_thirteen_boundaries(self):
        self.activate()
        sent = []
        for hour in range(24):
            # Local hour mapped to UTC on the same test day.
            instant = datetime(2026, 10, 23, hour, tzinfo=UTC) - timedelta(hours=3)
            p = self.diary.schedule(instant)
            if p:
                sent.append(p)
                self.assertIsNone(self.diary.schedule(instant + timedelta(seconds=20)))
        self.assertEqual(len(sent), 13)
        self.assertIn('08:00 до 09:00', sent[0]['text'])
        self.assertIn('20:00 до 21:00', sent[-1]['text'])
        self.assertIsNone(self.diary.schedule(NOW + timedelta(minutes=1)))

    def test_no_catchup_after_gap(self):
        self.activate()
        self.assertIsNone(self.diary.schedule(NOW + timedelta(hours=4, minutes=4)))
        p = self.diary.schedule(NOW + timedelta(hours=5))
        self.assertIn('13:00 до 14:00', p['text'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM prompts').fetchone()[0], 1)

    def test_pause_resume_persistent_and_next_hour(self):
        self.activate()
        self.apply(self.update('/pause'))
        self.assertIsNone(self.diary.schedule(NOW))
        self.store.close()
        self.store = Store(self.cfg.db_path)
        self.diary = Diary(self.store, self.cfg)
        self.assertIsNone(self.diary.schedule(NOW))
        self.apply(self.update('/resume'))
        self.assertIsNone(self.diary.schedule(NOW + timedelta(seconds=10)))
        self.assertIsNotNone(self.diary.schedule(NOW + timedelta(hours=1)))

    def test_free_text_is_full_and_not_last_period(self):
        self.prompt()
        self.apply(self.update('  исходный\n<!-- hourly-diary:end -->\n/не команда  ', when=NOW + timedelta(hours=15)))
        e = self.rows()[0]
        self.assertIsNone(e['prompt_id'])
        self.assertEqual(e['day'], '2026-10-24')
        self.assertTrue(e['text'].startswith('  исходный'))

    def test_late_replies_and_multiple_additions(self):
        self.prompt()
        a = self.update('ответ', reply=900, when=NOW + timedelta(days=1))
        self.apply(a)
        self.apply(self.update('ещё', reply=a['message']['message_id'], when=NOW + timedelta(days=2)))
        self.apply(self.update('третье', reply=900))
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual({r['day'] for r in rows}, {'2026-10-23'})
        self.assertEqual(len({r['prompt_id'] for r in rows}), 1)
        self.assertTrue(any(r['parent_id'] for r in rows))

    def test_edited_text_keeps_original_and_revisions(self):
        u = self.update('исходник')
        self.apply(u)
        self.apply(self.update('правка', mid=u['message']['message_id'], kind='edited_message'))
        e = self.rows()[0]
        self.assertEqual((e['original_text'], e['text'], e['version']), ('исходник', 'правка', 2))
        self.assertEqual(len(e['revisions']), 2)

    def test_update_durable_before_offset_and_restart(self):
        u = self.update('durable')
        self.diary.receive(u)
        self.assertEqual(self.store.get_setting('offset'), u['update_id'] + 1)
        self.store.close()
        self.store = Store(self.cfg.db_path)
        self.diary = Diary(self.store, self.cfg)
        for update in self.store.pending_updates():
            self.diary.process(update)
        self.apply(u)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(self.store.pending_updates())

    def test_message_duplicate_different_update(self):
        u = self.update('once')
        self.apply(u)
        self.apply(self.update('once', mid=u['message']['message_id']))
        self.assertEqual(len(self.rows()), 1)

    def test_unknown_send_not_repeated_after_restart(self):
        self.activate()
        p = self.diary.schedule(NOW)
        self.store.recover()
        self.assertIsNone(self.diary.schedule(NOW))
        self.assertEqual(self.store.db.execute('SELECT state FROM prompts WHERE id=?', (p['id'],)).fetchone()[0], 'unknown')
        self.apply(self.update('не потерять', reply=989))
        self.assertEqual(self.rows()[0]['reply_message_id'], 989)

    def test_reply_arrives_before_send_result_reconciles_descendants(self):
        self.activate()
        prompt = self.diary.schedule(NOW)
        first = self.update('быстрый поздний ответ', reply=900, when=NOW + timedelta(days=1))
        self.apply(first)
        self.apply(self.update('дочернее дополнение', reply=first['message']['message_id'], when=NOW + timedelta(days=2)))
        self.assertTrue(all(r['prompt_id'] is None for r in self.rows()))
        self.assertEqual({r['day'] for r in self.rows()}, {'2026-10-24'})
        self.diary.sent_prompt(prompt['id'], 900)
        rows = self.rows()
        self.assertEqual({r['prompt_id'] for r in rows}, {prompt['id']})
        self.assertEqual({r['day'] for r in rows}, {'2026-10-23'})
        self.assertTrue(all(r['version'] == 2 for r in rows))
        self.diary.sent_prompt(prompt['id'], 900)
        self.assertTrue(all(r['version'] == 2 for r in self.rows()))

    def test_sync_request_is_owner_only_deduplicated_and_statused(self):
        self.apply(self.update('/sync', owner=999))
        self.assertIsNone(self.store.latest_sync())
        self.apply(self.update('/sync'))
        first = self.store.latest_sync()
        self.assertEqual(first['state'], 'requested')
        self.apply(self.update('/sync'))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM sync_requests').fetchone()[0], 1)
        status = self.store.response()
        self.assertIn('Синхронизация', status['text'])
        self.store.finish_response(status['update_id'], 'done')
        duplicate = self.store.response()
        self.assertIsNotNone(duplicate)
        self.store.finish_response(duplicate['update_id'], 'done')
        self.apply(self.update('/status'))
        status = self.store.response()
        self.assertIn('Obsidian: запрошена', status['text'])

    def test_command_response_dedup_and_range(self):
        self.apply(self.update('/export'))
        hint = self.store.response()
        self.assertIsNone(hint['export_start'])
        self.assertIn('сегодня', hint['text'])
        self.store.finish_response(hint['update_id'], 'done')
        self.apply(self.update('/export вчера'))
        yesterday = self.store.response()
        self.assertEqual((yesterday['export_start'], yesterday['export_end']), ('2026-10-22', '2026-10-22'))
        self.store.finish_response(yesterday['update_id'], 'done')
        self.apply(self.update('/today'))
        today = self.store.response()
        self.assertEqual((today['export_start'], today['export_end']), ('2026-10-23', '2026-10-23'))
        self.store.finish_response(today['update_id'], 'done')
        self.apply(self.update('/export 2026-10-20 2026-10-23'))
        r = self.store.response()
        self.assertEqual((r['export_start'], r['export_end']), ('2026-10-20', '2026-10-23'))
        self.store.finish_response(r['update_id'], 'done')
        self.apply(self.update('/export 2026-10-23 2026-10-01'))
        invalid = self.store.response()
        self.assertIsNone(invalid['export_start'])
        self.assertIn('Не понял период', invalid['text'])
        self.store.finish_response(invalid['update_id'], 'done')
        self.store.recover()
        self.assertIsNone(self.store.response())
        self.assertEqual(self.rows(), [])
        with self.assertRaises(ValueError):
            parse_range('2026-10-23 2026-10-01', NOW.date(), 31)
        with self.assertRaises(ValueError):
            parse_range('2026-01-01 2026-12-31', NOW.date(), 31)

    def test_aiogram_update_shape(self):
        from aiogram.types import Update
        u = self.update('aiogram-compatible')
        u['message']['from']['is_bot'] = False
        parsed = Update.model_validate(u)
        serialized = parsed.model_dump(mode='json', exclude_none=True, by_alias=True)
        self.apply(serialized)
        self.assertEqual(self.rows()[0]['text'], 'aiogram-compatible')

    def test_runtime_durable_poll_and_text_processing_with_fake_bot(self):
        from aiogram.types import Update
        from hourly_diary.runtime import serve
        raw = self.update('runtime fixture')
        raw['message']['from']['is_bot'] = False
        item = Update.model_validate(raw)
        path = self.cfg.db_path
        offsets = []
        class Session:
            async def close(self): pass
        class FakeBot:
            session = Session()
            calls = 0
            async def get_updates(self, offset, **kwargs):
                offsets.append(offset)
                self.calls += 1
                if self.calls == 1:
                    return [item]
                db = sqlite3.connect(path)
                try:
                    stored = db.execute('SELECT value FROM settings WHERE key=\'offset\'').fetchone()
                    if not stored or json.loads(stored[0]) != item.update_id + 1:
                        raise AssertionError('Offset advanced without durable inbox')
                finally:
                    db.close()
                await asyncio.sleep(0.02)
                return []
            async def send_message(self, *args, **kwargs):
                class Sent:
                    message_id = 1000
                return Sent()
        async def exercise():
            try:
                await asyncio.wait_for(serve(self.cfg), 1.5)
            except asyncio.TimeoutError:
                pass
        with patch('aiogram.Bot', return_value=FakeBot()):
            asyncio.run(exercise())
        self.assertGreaterEqual(len(offsets), 2)
        self.assertEqual(offsets[1], item.update_id + 1)
        self.assertEqual(self.rows()[0]['text'], 'runtime fixture')

    def test_commands_match_eight_selected_names(self):
        from hourly_diary.commands import COMMANDS
        self.assertEqual([n for n, _ in COMMANDS], ['start', 'today', 'export', 'sync', 'pause', 'resume', 'status', 'correct', 'help'])


class FakeDownloader:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    async def download(self, file_id, destination, limit):
        self.calls += 1
        if self.error:
            raise self.error
        destination.write_bytes(b'OggSfake')


class FakeSTT:
    def __init__(self, error=None):
        self.error = error

    async def transcribe(self, path):
        if self.error:
            raise self.error
        return 'машинный текст без пересказа'


class MediaTests(Base):
    def worker(self, stt=None, download=None, config=None):
        return MediaWorker(self.store, config or self.cfg, download or FakeDownloader(), stt or FakeSTT())

    def voice(self):
        self.prompt()
        u = self.update(reply=900, voice=True)
        self.apply(u)
        return u

    def test_voice_download_transcribe_correct_and_export(self):
        u = self.voice()
        worker = self.worker()
        asyncio.run(worker.step(1))
        asyncio.run(worker.step(2))
        self.apply(self.update('/correct точный текст', reply=u['message']['message_id']))
        e = self.rows()[0]
        self.assertEqual(e['status'], 'transcribed')
        self.assertEqual(e['transcript'], 'машинный текст без пересказа')
        self.assertEqual(e['corrected'], 'точный текст')
        self.assertEqual((self.data / e['audio_path']).read_bytes(), b'OggSfake')
        output = self.root / 'export.zip'
        bundle = build_export(self.store, self.cfg, output)
        with zipfile.ZipFile(output) as z:
            self.assertEqual(z.read(e['audio_path']), b'OggSfake')
            data = json.loads(z.read('entries.json'))
            self.assertEqual(data['entries'][0]['transcript']['corrected'], 'точный текст')
            self.assertNotIn('file_id', data['entries'][0])
        self.assertTrue(bundle['complete'])

    def test_stt_unavailable_retains_audio_and_retry(self):
        self.voice()
        worker = self.worker(FakeSTT(MediaError('stt_unavailable')))
        asyncio.run(worker.step(1))
        asyncio.run(worker.step(2))
        e = self.rows()[0]
        self.assertEqual(e['status'], 'stt_unavailable')
        self.assertTrue((self.data / e['audio_path']).is_file())
        self.apply(self.update('/retry'))
        asyncio.run(self.worker().step(3))
        self.assertEqual(self.rows()[0]['status'], 'transcribed')

    def test_retry_backoff_limit_and_restart(self):
        self.voice()
        downloader = FakeDownloader(MediaError('download_network_error', True))
        worker = self.worker(download=downloader)
        asyncio.run(worker.step(1))
        self.assertFalse(asyncio.run(worker.step(2)))
        for now in (10000, 20000, 30000, 40000):
            asyncio.run(worker.step(now))
        self.assertEqual(downloader.calls, 5)
        self.assertFalse(asyncio.run(worker.step(50000)))
        self.assertEqual(self.rows()[0]['file_id'], 'opaque-fixture-id')
        self.assertEqual(self.rows()[0]['status'], 'download_error')

    def test_claimed_job_recovers(self):
        self.voice()
        self.assertIsNotNone(self.store.job(1))
        self.store.recover()
        self.assertIsNotNone(self.store.job(2))

    def test_capacity_error_is_not_silent(self):
        self.voice()
        worker = self.worker(config=replace(self.cfg, storage_limit_bytes=1))
        asyncio.run(worker.step(1))
        self.assertEqual(self.rows()[0]['error_code'], 'capacity_error')
        self.assertIsNone(self.rows()[0]['audio_path'])

    def test_audio_size_limit(self):
        self.voice()
        worker = self.worker(config=replace(self.cfg, max_audio_bytes=1))
        asyncio.run(worker.step(1))
        self.assertEqual(self.rows()[0]['error_code'], 'audio_too_large')

    def test_http_adapter_user_agent_model_sanitized_403(self):
        class Content:
            async def iter_chunked(self, limit):
                yield b'{"text":'
                yield b'"fixture transcription"}'
        class Response:
            status = 200
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class Session:
            response = Response()
            def post(self, endpoint, **kwargs):
                self.endpoint, self.kwargs = endpoint, kwargs
                return self.response
        session = Session()
        path = self.root / 'fixture.ogg'
        path.write_bytes(b'OggS')
        config = replace(self.cfg, stt_provider='groq', stt_api_key='fake-for-unit-test')
        adapter = HTTPTranscriber(config, session)
        self.assertEqual(asyncio.run(adapter.transcribe(path)), 'fixture transcription')
        self.assertIn('User-Agent', session.kwargs['headers'])
        self.assertEqual(session.endpoint, config.endpoint)
        session.response.status = 403
        with self.assertRaises(MediaError) as raised:
            asyncio.run(adapter.transcribe(path))
        self.assertEqual(str(raised.exception), 'stt_auth')
        self.assertNotIn('fake-for-unit-test', str(raised.exception))
        self.assertFalse(raised.exception.retryable)

    def test_http_chunk_limit_and_invalid_json_not_retried(self):
        class Content:
            chunks = [b'{"text":', b'broken']
            async def iter_chunked(self, limit):
                for chunk in self.chunks:
                    yield chunk
        class Response:
            status = 200
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class Session:
            def post(self, *args, **kwargs): return Response()
        path = self.root / 'test.ogg'
        path.write_bytes(b'OggS')
        adapter = HTTPTranscriber(replace(self.cfg, stt_provider='groq', stt_api_key='fake'), Session())
        for chunks in ([b'{"text":', b'broken'], [b'x' * (2 * 1024 * 1024), b'x']):
            Content.chunks = chunks
            with self.assertRaises(MediaError) as error:
                asyncio.run(adapter.transcribe(path))
            self.assertEqual(error.exception.code, 'stt_invalid_response')
            self.assertFalse(error.exception.retryable)

    def test_no_stt_network_without_access(self):
        class NoNetwork:
            def post(self, *args, **kwargs):
                raise AssertionError('Network must not be called')
        with self.assertRaises(MediaError) as raised:
            asyncio.run(HTTPTranscriber(self.cfg, NoNetwork()).transcribe(Path('nonexistent')))
        self.assertEqual(raised.exception.code, 'stt_unavailable')


class ExportSyncBackupTests(Base):
    def test_export_complete_text_free_reply_edits(self):
        self.prompt()
        self.apply(self.update('точный текст\nс переносом', reply=900))
        self.apply(self.update('свободное'))
        output = self.root / 'bundle.zip'
        bundle = build_export(self.store, self.cfg, output)
        self.assertEqual(len(bundle['entries']), 2)
        self.assertTrue(bundle['entries'][0]['period'])
        with zipfile.ZipFile(output) as z:
            manifest = json.loads(z.read('manifest.json'))['files']
            for name, digest in manifest.items():
                self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(), digest)
            self.assertIn('точный текст', z.read('diary.md').decode())
        with self.assertRaises(ValueError):
            build_export(self.store, self.cfg, self.root / 'small.zip', limit=1)
        self.assertFalse((self.root / 'small.zip.part').exists())

    def test_sync_preserves_manual_and_updates_versions(self):
        u = self.update('original <!-- hourly-diary:end -->')
        self.apply(u)
        vault = self.root / 'vault'
        note = vault / 'Daily' / '2026-10-23.md'
        note.parent.mkdir(parents=True)
        note.write_text('# Мой день\nРучной раздел\n', encoding='utf-8')
        output = self.root / 'bundle.zip'
        build_export(self.store, self.cfg, output)
        result = import_bundle(output, vault)
        self.assertEqual(result['days_changed'], 1)
        first = note.read_bytes()
        self.assertEqual(import_bundle(output, vault)['days_changed'], 0)
        self.assertEqual(note.read_bytes(), first)
        self.assertIn('Ручной раздел', first.decode())
        self.assertEqual(first.decode().count('<!-- hourly-diary:end -->'), 1)
        self.apply(self.update('new text', mid=u['message']['message_id'], kind='edited_message'))
        stale = self.root / 'stale.zip'
        stale.write_bytes(output.read_bytes())
        build_export(self.store, self.cfg, output)
        import_bundle(output, vault)
        self.assertIn('new text', note.read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):
            import_bundle(stale, vault)

    def test_sync_reconciled_reply_moves_day_without_duplicate(self):
        self.activate()
        prompt = self.diary.schedule(NOW)
        self.apply(self.update('pending reply', reply=900, when=NOW + timedelta(days=1)))
        output = self.root / 'bundle.zip'
        vault = self.root / 'vault'
        build_export(self.store, self.cfg, output)
        import_bundle(output, vault)
        old = vault / 'Daily' / '2026-10-24.md'
        self.assertIn('pending reply', old.read_text(encoding='utf-8'))
        self.diary.sent_prompt(prompt['id'], 900)
        build_export(self.store, self.cfg, output)
        import_bundle(output, vault)
        self.assertNotIn('pending reply', old.read_text(encoding='utf-8'))
        self.assertIn('pending reply', (vault / 'Daily' / '2026-10-23.md').read_text(encoding='utf-8'))

    def test_sync_audio_and_incomplete_visible(self):
        self.apply(self.update(voice=True))
        worker = MediaWorker(self.store, self.cfg, FakeDownloader(), FakeSTT(MediaError('stt_unavailable')))
        asyncio.run(worker.step(1))
        asyncio.run(worker.step(2))
        output = self.root / 'bundle.zip'
        build_export(self.store, self.cfg, output)
        vault = self.root / 'vault'
        import_bundle(output, vault)
        audio = vault / 'Attachments' / 'hourly-diary' / (self.rows()[0]['id'] + '.ogg')
        self.assertEqual(audio.read_bytes(), b'OggSfake')
        self.assertIn('stt_unavailable', (vault / 'Daily' / '2026-10-23.md').read_text(encoding='utf-8'))
        (self.data / self.rows()[0]['audio_path']).unlink()
        self.assertFalse(build_export(self.store, self.cfg, output)['complete'])

    def test_sync_conflicting_markers(self):
        with self.assertRaises(ValueError):
            merge_note('<!-- hourly-diary:start -->broken', 'body')
        with self.assertRaises(ValueError):
            merge_note('<!-- hourly-diary:end --><!-- hourly-diary:start -->', 'body')

    def test_sync_detects_checksum_corruption(self):
        self.apply(self.update('text'))
        output = self.root / 'bundle.zip'
        build_export(self.store, self.cfg, output)
        bad = self.root / 'bad.zip'
        with zipfile.ZipFile(output) as source, zipfile.ZipFile(bad, 'w') as dest:
            for name in source.namelist():
                dest.writestr(name, b'changed' if name == 'diary.md' else source.read(name))
        with self.assertRaises(ValueError):
            import_bundle(bad, self.root / 'vault')

    def test_backup_restore_sqlite_and_audio_and_corruption(self):
        self.apply(self.update('back me up'))
        self.apply(self.update(voice=True))
        asyncio.run(MediaWorker(self.store, self.cfg, FakeDownloader(), FakeSTT()).step(1))
        output = self.root / 'backups'
        generation = backup(self.data, output)
        generation2 = backup(self.data, output)
        self.assertEqual(len(list((output / 'objects').iterdir())), 1)
        target = self.root / 'restored'
        restore(generation, target)
        integrity(target / 'diary.sqlite3')
        restored = Store(target / 'diary.sqlite3')
        try:
            self.assertEqual(len(restored.entries('0001-01-01', '9999-12-31')), 2)
        finally:
            restored.close()
        self.assertTrue((target / next(r['audio_path'] for r in self.rows() if r['kind'] == 'voice')).exists())
        with self.assertRaises(ValueError):
            restore(generation, target)
        (generation2 / 'diary.sqlite3').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            restore(generation2, self.root / 'bad-restore')
        self.assertFalse((self.root / 'bad-restore').exists())

    def test_installed_package_runs_outside_project(self):
        import subprocess
        import sys
        proc = subprocess.run([sys.executable, '-I', '-m', 'hourly_diary', 'commands'],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(json.loads(proc.stdout)), 9)
        snapshot_file = self.root / 'outside.zip'
        self.apply(self.update('outside cwd'))
        proc = subprocess.run([sys.executable, '-I', '-m', 'hourly_diary', 'snapshot',
                               '--data-dir', str(self.data), '--output', str(snapshot_file)],
                              cwd=self.root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(snapshot_file.is_file())

    def test_pull_uses_installed_module_no_cwd_assumption(self):
        from hourly_diary.sync import pull
        import shlex
        calls = []
        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'scp':
                self.apply(self.update('pull fixture'))
                build_export(self.store, self.cfg, Path(argv[-1]))
        with patch('hourly_diary.sync.subprocess.run', side_effect=fake_run):
            result = pull('example-host', self.root / 'vault')
        self.assertEqual(result['entries'], 1)
        remote = shlex.split(calls[0][-1])
        self.assertEqual(remote[:4], ['runuser', '-u', 'hourlydiary', '--'])
        self.assertEqual(remote[4:8], ['/opt/hourly-diary/venv/bin/python', '-m', 'hourly_diary', 'snapshot'])
        self.assertIn('StrictHostKeyChecking=yes', calls[0])
        self.assertNotIn('shell=True', repr(calls))

    def test_sync_poll_idle_does_not_download_or_modify_vault(self):
        from hourly_diary.sync import sync_once
        vault = self.root / 'vault'
        with patch('hourly_diary.sync.subprocess.check_output', return_value='{"result":"idle"}') as check:
            result = sync_once('example-host', vault)
        self.assertEqual(result, {'result': 'idle'})
        check.assert_called_once()
        self.assertFalse(vault.exists())

    def test_offhost_backup_pull_restore_checked(self):
        from hourly_diary.offhost import pull_backup
        import shutil
        self.apply(self.update('offhost fixture'))
        source = self.root / 'server-backups'
        backup(self.data, source)
        def fake_run(argv, **kwargs):
            if argv[0] == 'scp':
                shutil.copytree(source, Path(argv[-1]))
        with patch('hourly_diary.offhost.subprocess.run', side_effect=fake_run):
            receipt = pull_backup('example-host', self.root / 'independent')
        self.assertTrue(receipt['restore_checked'])
        self.assertEqual(receipt['generations'], 1)
        self.assertTrue((self.root / 'independent' / 'OFFHOST_RECEIPT.json').is_file())

    def test_cross_process_poll_lock(self):
        import subprocess
        import sys
        path = self.root / 'poll.lock'
        code = 'from hourly_diary.runtime import process_lock; import sys;\nwith process_lock(sys.argv[1]): pass'
        with process_lock(path):
            result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
