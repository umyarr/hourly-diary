"""Consistent private exports, streaming ZIP audio; no credentials in output."""
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import zipfile
from zoneinfo import ZoneInfo


def audio_source(data_dir, relative):
    if not relative or not re.fullmatch(r'audio/[a-f0-9]{32}\.ogg', relative):
        raise ValueError('Invalid audio reference')
    source = Path(data_dir) / relative
    if source.is_symlink() or source.resolve().parent != (Path(data_dir) / 'audio').resolve():
        raise ValueError('Unsafe audio reference')
    return source


def file_sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(store, config, start='0001-01-01', end='9999-12-31'):
    rows = store.entries(start, end)
    entries = []
    for row in rows:
        period = {'start': row['period_start'], 'end': row['period_end']} if row['period_start'] else None
        entries.append({
            'id': row['id'], 'version': row['version'], 'kind': row['kind'], 'local_date': row['day'],
            'period': period, 'message_at': row['message_at'], 'received_at': row['received_at'],
            'edited_at': row['edited_at'], 'source_message_id': row['message_id'],
            'source_reply_message_id': row['reply_message_id'],
            'unresolved_reply': bool(row['reply_message_id'] and not row['prompt_id'] and not row['parent_id']),
            'parent_entry_id': row['parent_id'], 'original_text': row['original_text'],
            'text': row['text'], 'original_unknown': bool(row['original_unknown']),
            'revisions': row['revisions'],
            'audio': {'path': row['audio_path'], 'sha256': row['audio_sha256'],
                      'size': row['audio_size'], 'duration': row['duration'], 'mime_type': row['mime_type'],
                      'state': row['status'], 'included': False} if row['kind'] == 'voice' else None,
            'transcript': {'machine': row['transcript'], 'corrected': row['corrected'],
                           'state': row['status'], 'error_code': row['error_code']} if row['kind'] == 'voice' else None,
        })
    return {'schema_version': 1, 'timezone': config.timezone, 'range': {'start': start, 'end': end},
            'generated_at': datetime.now(timezone.utc).isoformat(), 'audio_included': True,
            'complete': True, 'entries': entries}


def literal(text):
    # Quote each line; escape Markdown/HTML so text cannot inject control markers.
    escaped = html.escape(text or '', quote=False)
    escaped = re.sub(r'([\\`*_{}\[\]()#+.!|~-])', r'\\\1', escaped)
    return '\n'.join('> ' + line for line in escaped.split('\n'))


def render_markdown(entries, timezone_name, audio_prefix='audio/'):
    tz = ZoneInfo(timezone_name)
    output = []
    for e in entries:
        stamp = datetime.fromisoformat(e['message_at']).astimezone(tz)
        if e['period']:
            start = datetime.fromisoformat(e['period']['start']).astimezone(tz)
            end = datetime.fromisoformat(e['period']['end']).astimezone(tz)
            label = f'{start:%H:%M}–{end:%H:%M}'
        else:
            label = 'Свободная запись'
        output += [f"<!-- entry:{e['id']}:v{e['version']} -->", f'### {label} · {stamp:%H:%M} · {e["kind"]}']
        output += [f'Время ответа: {stamp.isoformat()}']
        if e['parent_entry_id']:
            output += ['Дополнение к записи ' + e['parent_entry_id']]
        if e['kind'] == 'text':
            output += [literal(e['text'])]
            if e['text'] != e['original_text']:
                output += ['Первоначальный текст:', literal(e['original_text'])]
        else:
            audio = e['audio']
            output += ['Аудио: ' + (f"[исходник]({audio_prefix}{e['id']}.ogg)" if audio['included'] else 'не включено / недоступно')]
            tr = e['transcript']
            output += ['Статус: ' + tr['state']]
            if tr['machine'] is not None:
                output += ['Машинный транскрипт:', literal(tr['machine'])]
            if tr['corrected'] is not None:
                output += ['Исправление пользователя:', literal(tr['corrected'])]
            if tr['error_code']:
                output += ['Ошибка: ' + tr['error_code']]
        output += ['']
    return '\n'.join(output) if output else 'Записей пока нет.\n'


def build_export(store, config, destination, start='0001-01-01', end='9999-12-31', limit=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.part')
    bundle = snapshot(store, config, start, end)
    manifest = {}
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            for e in bundle['entries']:
                audio = e['audio']
                if audio is None:
                    continue
                try:
                    source = audio_source(config.data_dir, audio['path'])
                    if not source.is_file() or file_sha(source) != audio['sha256']:
                        raise ValueError('Audio missing or changed')
                    output.write(source, audio['path'])
                    audio['included'] = True
                    manifest[audio['path']] = audio['sha256']
                except (OSError, ValueError):
                    bundle['complete'] = False
                if limit and temporary.stat().st_size > limit:
                    raise ValueError('Export exceeds limit; use local sync')
            days = sorted({e['local_date'] for e in bundle['entries']})
            markdown = '\n'.join('# ' + day + '\n\n' + render_markdown(
                [e for e in bundle['entries'] if e['local_date'] == day], config.timezone) for day in days)
            for name, content in [('diary.md', markdown or 'Записей пока нет.\n'),
                                  ('entries.json', json.dumps(bundle, ensure_ascii=True, indent=2))]:
                data = content.encode('utf-8')
                output.writestr(name, data)
                manifest[name] = hashlib.sha256(data).hexdigest()
            output.writestr('manifest.json', json.dumps({'schema_version': 1, 'files': manifest}, indent=2))
        if limit and temporary.stat().st_size > limit:
            raise ValueError('Export exceeds limit; use local sync')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return bundle
