"""Aiogram transport with manual durable polling (no Dispatcher offset race)."""
import asyncio
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import tempfile
from .config import Config
from .core import Diary, utc_now
from .exporting import build_export
from .media import HTTPTranscriber, MediaError, MediaWorker
from .store import Store


@contextmanager
def process_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


class TelegramTransport:
    def __init__(self, bot, session, token):
        self.bot, self.session, self.token = bot, session, token

    async def download(self, file_id, destination, limit):
        import aiohttp
        try:
            file = await self.bot.get_file(file_id)
            if file.file_size and file.file_size > limit:
                raise MediaError('audio_too_large')
            if not file.file_path:
                raise MediaError('download_unavailable', True)
            # URL deliberately remains inside the adapter, never in reports/logs.
            url = 'https://api.telegram.org/file/bot' + self.token + '/' + file.file_path
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=90)) as response:
                if response.status != 200:
                    raise MediaError('download_http_error', response.status == 429 or response.status >= 500)
                total = 0
                with open(destination, 'wb') as output:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise MediaError('audio_too_large')
                        await asyncio.to_thread(output.write, chunk)
        except MediaError:
            raise
        except Exception:
            # Aiogram exception reprs may contain request data. Never propagate.
            raise MediaError('download_network_error', True) from None


async def serve(config):
    import aiohttp
    from aiogram import Bot
    from aiogram.types import FSInputFile, KeyboardButton, ReplyKeyboardMarkup
    from .commands import BUTTONS
    if not config.bot_token:
        raise ValueError('BOT_TOKEN is required')
    store = Store(config.db_path)
    store.recover()
    diary = Diary(store, config)
    bot = Bot(config.bot_token)
    keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in BUTTONS], resize_keyboard=True)
    try:
        async with aiohttp.ClientSession() as http:
            media = MediaWorker(store, config, TelegramTransport(bot, http, config.bot_token), HTTPTranscriber(config, http))

            async def poll():
                while True:
                    try:
                        updates = await bot.get_updates(offset=await asyncio.to_thread(store.get_setting, 'offset', 0),
                                                        timeout=25, allowed_updates=['message', 'edited_message'], request_timeout=35)
                    except Exception:
                        print('poll_network_error', flush=True)
                        await asyncio.sleep(5)
                        continue
                    for update in updates:
                        # Commit inbox AND next offset before issuing next poll.
                        await asyncio.to_thread(diary.receive, update.model_dump(mode='json', exclude_none=True, by_alias=True))

            async def process():
                while True:
                    updates = await asyncio.to_thread(store.pending_updates)
                    for update in updates:
                        await asyncio.to_thread(diary.process, update)
                    await asyncio.sleep(0.2 if updates else 1)

            async def reminders():
                while True:
                    prompt = await asyncio.to_thread(diary.schedule)
                    if prompt:
                        if utc_now().isoformat() >= prompt['deadline']:
                            with store.transaction() as c:
                                c.execute("UPDATE prompts SET state='skipped' WHERE id=?", (prompt['id'],))
                            continue
                        try:
                            # Never retry an uncertain send; stable slot survives restart.
                            message = await bot.send_message(prompt['chat_id'], prompt['text'], reply_markup=keyboard)
                            await asyncio.to_thread(diary.sent_prompt, prompt['id'], message.message_id)
                        except Exception:
                            await asyncio.to_thread(diary.sent_prompt, prompt['id'])
                    await asyncio.sleep(1)

            async def replies():
                while True:
                    response = await asyncio.to_thread(store.response)
                    if not response:
                        await asyncio.sleep(0.5)
                        continue
                    state = 'unknown'
                    try:
                        if response['export_start']:
                            export_dir = config.data_dir / 'exports'
                            export_dir.mkdir(parents=True, exist_ok=True)
                            # Private ephemeral export, never a permanent public URL.
                            with tempfile.TemporaryDirectory(prefix='request-', dir=export_dir) as directory:
                                archive = Path(directory) / f"hourly-diary-{response['export_start']}-{response['export_end']}.zip"
                                try:
                                    bundle = await asyncio.to_thread(build_export, store, config, archive,
                                                response['export_start'], response['export_end'], config.max_export_bytes)
                                    size_mib = archive.stat().st_size / (1024 * 1024)
                                except (OSError, ValueError):
                                    await bot.send_message(config.owner_id, f'Не удалось собрать ZIP. Проверь период (до {config.max_export_days} дней) и лимит {config.max_export_bytes // (1024 * 1024)} MiB.')
                                else:
                                    caption = (f'Выгрузка {response["export_start"]} — {response["export_end"]}. '
                                               f'Размер: {size_mib:.2f} MiB. Скачай этот ZIP в Telegram Desktop.'
                                               + ('' if bundle['complete'] else '\nЧасть аудио недоступна: подробный статус в JSON.'))
                                    await bot.send_document(config.owner_id, FSInputFile(archive), caption=caption, reply_markup=keyboard)
                        else:
                            await bot.send_message(config.owner_id, response['text'], reply_markup=keyboard)
                        state = 'sent'
                    except Exception:
                        # User may reissue command; do not duplicate uncertain sends.
                        pass
                    await asyncio.to_thread(store.finish_response, response['update_id'], state)

            async def voices():
                while True:
                    if not await media.step():
                        await asyncio.sleep(1)

            tasks = [asyncio.create_task(f()) for f in (poll, process, reminders, replies, voices)]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await bot.session.close()
        store.close()


def run(config):
    # Third-party exception logs must not print tokens or message bodies.
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    with process_lock(config.data_dir / 'polling.lock'):
        asyncio.run(serve(config))
