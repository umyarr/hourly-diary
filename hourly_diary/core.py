"""Pure application logic; no Telegram or STT network operations."""
from datetime import datetime, timedelta, timezone, date
import json
import uuid
from zoneinfo import ZoneInfo
from .commands import BUTTON_COMMANDS, HELP, WELCOME

UTC = timezone.utc


def utc_now():
    return datetime.now(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat()


def parse_range(argument, today, max_days):
    words = argument.strip().casefold().split()
    aliases = {'сегодня': today, 'today': today,
               'вчера': today - timedelta(days=1), 'yesterday': today - timedelta(days=1)}
    if not words:
        raise ValueError('Range is required')
    if len(words) == 1 and words[0] in aliases:
        start = end = aliases[words[0]]
    elif len(words) in (1, 2):
        try:
            start = date.fromisoformat(words[0])
            end = date.fromisoformat(words[-1])
        except ValueError:
            raise ValueError('Invalid range') from None
    else:
        raise ValueError('Invalid range')
    if end < start or (end - start).days + 1 > max_days:
        raise ValueError('Invalid range')
    return start.isoformat(), end.isoformat()


class Diary:
    def __init__(self, store, config):
        self.store = store
        self.config = config
        self.tz = ZoneInfo(config.timezone)
        with store.transaction() as c:
            store.set_setting(c, 'timezone', config.timezone)
            if config.chat_id and not store.setting(c, 'chat_id'):
                store.set_setting(c, 'chat_id', config.chat_id)

    def sanitize_update(self, update):
        """Unauthorized payloads never enter the durable inbox."""
        m = update.get('message') or update.get('edited_message') or {}
        if (m.get('from', {}).get('id') != self.config.owner_id
                or m.get('chat', {}).get('type') != 'private'
                or m.get('chat', {}).get('id') != self.config.owner_id):
            return {'update_id': update['update_id']}
        # Only required fields; strip names, handles, phone numbers in metadata,
        # forwarded messages and embedded reply text (reply ID is sufficient).
        clean = {k: m[k] for k in ('message_id', 'date', 'edit_date', 'text') if k in m}
        clean['from'] = {'id': self.config.owner_id}
        clean['chat'] = {'id': self.config.owner_id, 'type': 'private'}
        if 'reply_to_message' in m:
            clean['reply_to_message'] = {'message_id': m['reply_to_message']['message_id']}
        if 'voice' in m:
            clean['voice'] = {k: m['voice'][k] for k in
                              ('file_id', 'file_unique_id', 'duration', 'file_size', 'mime_type') if k in m['voice']}
        return {'update_id': update['update_id'],
                'edited_message' if 'edited_message' in update else 'message': clean}

    def receive(self, update):
        self.store.enqueue(self.sanitize_update(update))

    def process(self, update, now=None):
        now = now or utc_now()
        uid = update['update_id']
        with self.store.transaction() as c:
            stored = c.execute('SELECT done FROM inbox WHERE update_id=?', (uid,)).fetchone()
            if not stored or stored[0]:
                return
            m = update.get('message') or update.get('edited_message')
            if m:
                response, export = self._message(c, m, uid, now, 'edited_message' in update)
                if response is not None:
                    c.execute('INSERT OR IGNORE INTO responses(update_id,text,export_start,export_end) VALUES (?,?,?,?)',
                              (uid, response, export[0] if export else None, export[1] if export else None))
            # Remove processed raw message body; only IDs retained for dedup.
            c.execute("UPDATE inbox SET done=1,body=? WHERE update_id=?", (json.dumps({'update_id': uid}), uid))

    def _message(self, c, m, uid, now, edited):
        owner = self.config.owner_id
        if (m.get('from', {}).get('id') != owner or m.get('chat', {}).get('id') != owner
                or m.get('chat', {}).get('type') != 'private'):
            return None, None
        text = m.get('text')
        existing = c.execute('SELECT * FROM entries WHERE message_id=?', (m['message_id'],)).fetchone()
        if existing and not edited:
            return None, None
        if edited and existing:
            if text is not None and existing['kind'] == 'text':
                version = existing['version'] + 1
                c.execute('UPDATE entries SET text=?,edited_at=?,version=? WHERE id=?',
                          (text, iso(datetime.fromtimestamp(m.get('edit_date', now.timestamp()), UTC)), version, existing['id']))
                c.execute('INSERT INTO revisions VALUES (?,?,?,?)', (existing['id'], version, uid,
                          json.dumps({'text': text, 'edited_at': m.get('edit_date')}, ensure_ascii=True)))
            return None, None
        effective = BUTTON_COMMANDS.get(text, text) if text is not None else ''
        command, _, argument = effective.partition(' ')
        command = command.split('@')[0]
        reply = m.get('reply_to_message', {}).get('message_id')
        if not edited and command.startswith('/'):
            if command == '/start':
                self.store.set_setting(c, 'chat_id', owner)
                return WELCOME, None
            if command in ('/pause', '/resume'):
                self.store.set_setting(c, 'paused', command == '/pause')
                if command == '/resume':
                    self.store.set_setting(c, 'chat_id', owner)
                    # Do not catch up the boundary in the current minute.
                    self.store.set_setting(c, 'resume_after', iso(now))
                return ('Напоминания на паузе.' if command == '/pause' else 'Продолжим со следующего часа.'), None
            if command == '/help':
                return HELP, None
            if command == '/note':
                return 'Пришли текст или голос — сохраню отдельной записью. Ответь на вопрос, чтобы дополнить нужный час.', None
            if command == '/sync':
                sync, created = self.store.request_sync(iso(now))
                if created:
                    return 'Синхронизация с Obsidian запрошена. Локальный компьютер проверит команду и обновит заметки один раз.', None
                labels = {'requested': 'запрошена', 'claimed': 'выполняется', 'running': 'выполняется',
                          'completed': 'выполнена', 'error': 'ошибка'}
                return f"Синхронизация уже {labels.get(sync['state'], sync['state'])}. Повтор не создан.", None
            if command == '/status':
                states = c.execute('SELECT status,count(*) FROM entries WHERE kind=\'voice\' GROUP BY status').fetchall()
                paused = self.store.setting(c, 'paused', False)
                active = self.store.setting(c, 'chat_id') is not None
                labels = {'audio_pending': 'загрузка', 'stt_pending': 'распознавание',
                          'transcribed': 'распознано', 'download_error': 'ошибка загрузки',
                          'stt_error': 'ошибка распознавания', 'stt_unavailable': 'распознавание недоступно'}
                voice_status = ', '.join(f'{labels.get(r[0], "ожидание")}: {r[1]}' for r in states) or 'нет'
                unknown = c.execute("SELECT count(*) FROM prompts WHERE state='unknown'").fetchone()[0]
                sync = self.store.latest_sync()
                sync_text = 'нет запросов'
                if sync:
                    labels = {'requested': 'запрошена', 'claimed': 'выполняется', 'running': 'выполняется',
                              'completed': 'выполнена', 'error': 'ошибка'}
                    sync_text = labels.get(sync['state'], sync['state'])
                    if sync.get('error_code'):
                        sync_text += f" ({sync['error_code']})"
                return (f"{'Пауза' if paused else 'Включено' if active else 'Ожидаю /start'}. Ежедневно 09:00–21:00 ({self.config.timezone})."
                        f'\nГолосовые записи: {voice_status}.\nНеясных отправок вопросов: {unknown}.\nObsidian: {sync_text}.', None)
            if command in ('/today', '/export'):
                if command == '/export' and not argument.strip():
                    return (f'Выгрузка по запросу. Примеры:\n'
                            f'/export сегодня\n/export вчера\n'
                            f'/export 2026-10-23\n/export 2026-10-01 2026-10-23\n'
                            f'Период — до {self.config.max_export_days} дней включительно; '
                            f'лимит ZIP — {self.config.max_export_bytes // (1024 * 1024)} MiB. '
                            'Архив содержит Markdown, JSON и доступные исходные голосовые.'), None
                try:
                    period = parse_range('сегодня' if command == '/today' and not argument.strip() else argument,
                                         now.astimezone(self.tz).date(), self.config.max_export_days)
                except ValueError:
                    return (f'Не понял период. Используй /export сегодня, /export вчера, '
                            f'/export ГГГГ-ММ-ДД или две даты. Максимум {self.config.max_export_days} дней.'), None
                return 'Готовлю ZIP выбранного периода: Markdown, JSON и доступные исходные голосовые.', period
            if command == '/correct':
                entry = c.execute("SELECT * FROM entries WHERE message_id=? AND kind='voice'", (reply,)).fetchone()
                if not entry or not argument.strip():
                    return 'Ответь на голос: /correct исправленный текст', None
                version = entry['version'] + 1
                c.execute('UPDATE entries SET corrected=?,version=? WHERE id=?', (argument, version, entry['id']))
                c.execute('INSERT INTO revisions VALUES (?,?,?,?)', (entry['id'], version, uid,
                          json.dumps({'corrected_transcript': argument}, ensure_ascii=True)))
                return 'Исправление сохранено. Исходный голос и распознавание остались.', None
            if command == '/retry':
                rows = c.execute("SELECT * FROM entries WHERE kind='voice' AND (? IS NULL OR message_id=?)", (reply, reply)).fetchall()
                count = 0
                for row in rows:
                    if row['status'] == 'transcribed':
                        continue
                    kind = 'stt' if row['audio_path'] else 'download'
                    queued = c.execute('''INSERT INTO jobs(kind,entry_id) VALUES (?,?)
                        ON CONFLICT(kind,entry_id) WHERE entry_id IS NOT NULL
                        DO UPDATE SET state='pending',attempts=0,retry_at=0
                        WHERE jobs.state NOT IN ('pending','running')''', (kind, row['id'])).rowcount
                    count += 1 if queued else 0
                if not count:
                    return 'Новых голосовых записей для повторной проверки нет.', None
                return f'Поставлено на повторную проверку: {count}. Результат — /status или выгрузка.', None
            # Unrecognized slash-prefixed text remains a free note; never lose it.
        if text is None and not m.get('voice'):
            return 'Для дневника пришли текст или голосовое сообщение.', None
        prompt_id = parent_id = None
        if reply:
            p = c.execute('SELECT id,day FROM prompts WHERE message_id=?', (reply,)).fetchone()
            parent = c.execute('SELECT id,prompt_id,day FROM entries WHERE message_id=?', (reply,)).fetchone()
            if p:
                prompt_id, day = p['id'], p['day']
            elif parent:
                parent_id, prompt_id, day = parent['id'], parent['prompt_id'], parent['day']
        moment = datetime.fromtimestamp(m['date'], UTC)
        if prompt_id is None and parent_id is None:
            day = moment.astimezone(self.tz).date().isoformat()
        entry_id = uuid.uuid4().hex
        voice = m.get('voice')
        status = 'audio_pending' if voice else 'saved'
        c.execute('''INSERT INTO entries(id,message_id,prompt_id,parent_id,reply_message_id,day,message_at,received_at,
            kind,original_text,text,file_id,file_unique_id,duration,audio_size,mime_type,status,original_unknown)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (entry_id, m['message_id'], prompt_id, parent_id, reply, day, iso(moment), iso(now),
                   'voice' if voice else 'text', text, text, voice.get('file_id') if voice else None,
                   voice.get('file_unique_id') if voice else None, voice.get('duration') if voice else None,
                   voice.get('file_size') if voice else None, voice.get('mime_type') if voice else None, status, int(edited)))
        c.execute('INSERT INTO revisions VALUES (?,?,?,?)', (entry_id, 1, uid,
                  json.dumps({'text': text, 'message_at': iso(moment), 'original_unknown': edited}, ensure_ascii=True)))
        if voice:
            c.execute("INSERT INTO jobs(kind,entry_id) VALUES ('download',?)", (entry_id,))
        # Interactive acknowledgments, not scheduled/out-of-hours push messages.
        return ('Голос получен. Сохраняю аудио; распознавание и ошибки будут видны в /today.'
                if voice else 'Записано.'), None

    def schedule(self, now=None):
        now = now or utc_now()
        local = now.astimezone(self.tz)
        if local.minute != 0 or not 9 <= local.hour <= 21:
            return None
        boundary = local.replace(minute=0, second=0, microsecond=0)
        start = boundary - timedelta(hours=1)
        with self.store.transaction() as c:
            if c.execute('SELECT 1 FROM inbox WHERE done=0 LIMIT 1').fetchone():
                return None  # Apply durable /pause before any new reminder after restart.
            if not self.store.setting(c, 'chat_id') or self.store.setting(c, 'paused', False):
                return None
            resumed = self.store.setting(c, 'resume_after')
            if resumed and iso(boundary) <= resumed:
                return None
            prompt_id = uuid.uuid4().hex
            inserted = c.execute('INSERT OR IGNORE INTO prompts(id,start,end,day,state) VALUES (?,?,?,?,?)',
                                 (prompt_id, iso(start), iso(boundary), start.date().isoformat(), 'sending')).rowcount
            if not inserted:
                return None
            return {'id': prompt_id, 'chat_id': self.store.setting(c, 'chat_id'),
                    'deadline': iso(boundary + timedelta(minutes=1)),
                    'text': f'Что происходило с {start:%H:%M} до {boundary:%H:%M}?\nОтветь текстом или голосом. Можно записать и то, что делаешь сейчас.'}

    def sent_prompt(self, prompt_id, message_id=None):
        with self.store.transaction() as c:
            c.execute('UPDATE prompts SET state=?,message_id=? WHERE id=?',
                      ('sent' if message_id is not None else 'unknown', message_id, prompt_id))
            if message_id is not None:
                prompt = c.execute('SELECT day FROM prompts WHERE id=?', (prompt_id,)).fetchone()
                if prompt:
                    # A reply can arrive while send_message is still awaiting its
                    # response. Reconcile by exact Telegram reply ID, never time;
                    # propagate through descendants that inherited the pending link.
                    c.execute('''WITH RECURSIVE linked(id) AS (
                        SELECT id FROM entries WHERE reply_message_id=? AND prompt_id IS NULL
                        UNION
                        SELECT child.id FROM entries child JOIN linked parent
                        ON child.parent_id=parent.id WHERE child.prompt_id IS NULL)
                        UPDATE entries SET prompt_id=?,day=?,version=version+1
                        WHERE id IN (SELECT id FROM linked)''', (message_id, prompt_id, prompt['day']))
