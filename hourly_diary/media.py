"""Durable media jobs. Network adapters may be replaced with fakes."""
import asyncio
import hashlib
import os
from pathlib import Path
import time
from .exporting import audio_source


class MediaError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class HTTPTranscriber:
    def __init__(self, config, session):
        self.config, self.session = config, session

    async def transcribe(self, path):
        if self.config.stt_provider == 'none' or not self.config.stt_api_key:
            raise MediaError('stt_unavailable')
        import aiohttp
        try:
            with open(path, 'rb') as stream:
                data = aiohttp.FormData()
                data.add_field('file', stream, filename='voice.ogg', content_type='audio/ogg')
                data.add_field('model', self.config.model)
                data.add_field('response_format', 'json')
                async with self.session.post(self.config.endpoint,
                        headers={'Authorization': 'Bearer ' + self.config.stt_api_key,
                                 'User-Agent': 'hourly-diary/0.1 (+private-voice-diary)'}, data=data,
                        timeout=aiohttp.ClientTimeout(total=120)) as response:
                    if response.status != 200:
                        code = 'stt_auth' if response.status in (401, 403) else 'stt_rate_limit' if response.status == 429 else 'stt_http_error'
                        raise MediaError(code, response.status == 429 or response.status >= 500)
                    # Limit provider output; do not log raw error bodies or text.
                    payload = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        payload.extend(chunk)
                        if len(payload) > 2 * 1024 * 1024:
                            raise MediaError('stt_invalid_response')
                    import json
                    try:
                        decoded = json.loads(payload)
                    except (ValueError, UnicodeError):
                        raise MediaError('stt_invalid_response') from None
                    text = decoded.get('text') if isinstance(decoded, dict) else None
                    if not isinstance(text, str):
                        raise MediaError('stt_invalid_response')
                    return text
        except MediaError:
            raise
        except (asyncio.TimeoutError, OSError, ValueError, aiohttp.ClientError):
            raise MediaError('stt_network_error', True) from None


def usage_bytes(root):
    # Account for database, retained backups and live audio. The sync archive
    # is a full derived copy of live data and exports are temporary, so counting
    # them would charge the same bytes twice and block voice uploads early.
    root = Path(root)
    total = 0
    for path in root.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        if path.relative_to(root).parts[0] in ('sync', 'exports'):
            continue
        total += path.stat().st_size
    return total


class MediaWorker:
    def __init__(self, store, config, downloader, transcriber):
        self.store, self.config = store, config
        self.downloader, self.transcriber = downloader, transcriber

    async def step(self, now=None):
        now = time.time() if now is None else now
        job = await asyncio.to_thread(self.store.job, now)
        if not job:
            return False
        entry = await asyncio.to_thread(self.store.get_entry, job['entry_id'])
        part = None
        try:
            if job['kind'] == 'download':
                if entry['audio_size'] and entry['audio_size'] > self.config.max_audio_bytes:
                    raise MediaError('audio_too_large')
                used = await asyncio.to_thread(usage_bytes, self.config.data_dir)
                if used + (entry['audio_size'] or self.config.max_audio_bytes) > self.config.storage_limit_bytes:
                    raise MediaError('capacity_error')
                folder = self.config.data_dir / 'audio'
                folder.mkdir(parents=True, exist_ok=True)
                final = folder / (entry['id'] + '.ogg')
                part = final.with_suffix('.part')
                await self.downloader.download(entry['file_id'], part, self.config.max_audio_bytes)
                size = part.stat().st_size
                if size > self.config.max_audio_bytes:
                    raise MediaError('audio_too_large')
                if not size:
                    raise MediaError('audio_empty', True)
                digest = await asyncio.to_thread(self._publish, part, final)
                await asyncio.to_thread(self.store.media_update, entry['id'],
                                        audio_path='audio/' + final.name, audio_sha256=digest,
                                        audio_size=size, status='stt_pending', error_code=None)
                with self.store.transaction() as c:
                    c.execute("INSERT OR IGNORE INTO jobs(kind,entry_id) VALUES ('stt',?)", (entry['id'],))
            elif job['kind'] == 'stt':
                source = audio_source(self.config.data_dir, entry['audio_path'])
                text = await self.transcriber.transcribe(source)
                await asyncio.to_thread(self.store.media_update, entry['id'], transcript=text,
                                        status='transcribed', error_code=None)
            await asyncio.to_thread(self.store.finish_job, job['id'])
        except MediaError as exc:
            pending = exc.retryable and job['attempts'] < 5
            status = 'download_error' if job['kind'] == 'download' else 'stt_error'
            if exc.code == 'stt_unavailable':
                status = 'stt_unavailable'
            await asyncio.to_thread(self.store.media_update, entry['id'], status=status, error_code=exc.code)
            await asyncio.to_thread(self.store.finish_job, job['id'], 'pending' if pending else 'failed',
                                    now + min(3600, 30 * 2 ** job['attempts']))
        except (OSError, ValueError):
            await asyncio.to_thread(self.store.media_update, entry['id'],
                                    status='download_error' if job['kind'] == 'download' else 'stt_error',
                                    error_code='local_io_error')
            await asyncio.to_thread(self.store.finish_job, job['id'], 'failed')
        finally:
            if part:
                part.unlink(missing_ok=True)
        return True

    @staticmethod
    def _publish(part, final):
        digest = hashlib.sha256()
        with part.open('rb') as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
        with part.open('ab') as f:
            f.flush()
            os.fsync(f.fileno())
        part.replace(final)
        return digest.hexdigest()
