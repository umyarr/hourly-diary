"""Windows-compatible OpenSSH pull and explicit-command Obsidian sync."""
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import zipfile
from .exporting import render_markdown
from .runtime import process_lock

START = '<!-- hourly-diary:start -->'
END = '<!-- hourly-diary:end -->'


def merge_note(original, body):
    if original.count(START) != original.count(END) or original.count(START) > 1:
        raise ValueError('Conflicting managed markers')
    block = START + '\n' + body.rstrip() + '\n' + END
    if START not in original:
        return original + ('\n\n' if original and not original.endswith('\n\n') else '') + block + '\n'
    a, b = original.index(START), original.index(END)
    if b < a:
        raise ValueError('Conflicting managed markers')
    return original[:a] + block + original[b + len(END):]


def atomic_note(path, body, moved_versions=None):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Note symlinks not supported')
    old_bytes = path.read_bytes() if path.exists() else None
    original = old_bytes.decode('utf-8') if old_bytes is not None else ''
    old_versions = dict((key, int(v)) for key, v in re.findall(r'<!-- entry:([a-f0-9]{32}):v(\d+) -->', original))
    new_versions = dict((key, int(v)) for key, v in re.findall(r'<!-- entry:([a-f0-9]{32}):v(\d+) -->', body))
    moved_versions = moved_versions or {}
    if any(max(new_versions.get(key, 0), moved_versions.get(key, 0)) < version for key, version in old_versions.items()):
        raise ValueError('Stale snapshot')
    updated = merge_note(original, body).encode('utf-8')
    if updated == old_bytes:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.hourly-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(updated); out.flush(); os.fsync(out.fileno())
        current = path.read_bytes() if path.exists() else None
        if current != old_bytes:
            raise ValueError('Concurrent note modification')
        if old_bytes is not None:
            backup_dir = path.parent / '.hourly-diary-backups'
            backup_dir.mkdir(exist_ok=True)
            (backup_dir / (path.name + '.before-sync')).write_bytes(old_bytes)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def import_bundle(archive, vault, max_bytes=600 * 1024 * 1024):
    vault = Path(vault); vault.mkdir(parents=True, exist_ok=True)
    with process_lock(vault / '.hourly-diary-sync.lock'), tempfile.TemporaryDirectory(prefix='diary-sync-') as temporary:
        stage = Path(temporary)
        with zipfile.ZipFile(archive) as z:
            members = z.infolist(); names = [m.filename for m in members]
            if len(names) != len(set(names)) or sum(m.file_size for m in members) > max_bytes:
                raise ValueError('Invalid or oversized bundle')
            if 'manifest.json' not in names or z.getinfo('manifest.json').file_size > 16 * 1024 * 1024:
                raise ValueError('Invalid manifest')
            manifest = json.loads(z.read('manifest.json'))
            if manifest.get('schema_version') != 1 or not isinstance(manifest.get('files'), dict):
                raise ValueError('Invalid manifest')
            checksums = manifest['files']
            if set(names) != set(checksums) | {'manifest.json'}:
                raise ValueError('Bundle member mismatch')
            for name, digest in checksums.items():
                if name not in ('entries.json', 'diary.md') and not re.fullmatch(r'audio/[a-f0-9]{32}\.ogg', name):
                    raise ValueError('Unsafe bundle path')
                if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
                    raise ValueError('Invalid checksum')
                member = z.getinfo(name)
                if member.is_dir() or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Bundle links forbidden')
                destination = stage / name; destination.parent.mkdir(parents=True, exist_ok=True)
                actual = hashlib.sha256()
                with z.open(member) as source, destination.open('wb') as output:
                    while chunk := source.read(1024 * 1024):
                        actual.update(chunk); output.write(chunk)
                if actual.hexdigest() != digest:
                    raise ValueError('Checksum mismatch')
        if not (stage / 'entries.json').is_file() or (stage / 'entries.json').stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Invalid entries')
        bundle = json.loads((stage / 'entries.json').read_text(encoding='utf-8'))
        if bundle.get('schema_version') != 1 or not isinstance(bundle.get('entries'), list):
            raise ValueError('Unsupported snapshot')
        receipt_file = vault / '.hourly-diary-receipt.json'
        previous_receipt = json.loads(receipt_file.read_text(encoding='utf-8')) if receipt_file.exists() else {}
        previous_versions = previous_receipt.get('versions', {})
        if any(e['version'] < previous_versions.get(e['id'], 0) for e in bundle['entries']):
            raise ValueError('Stale snapshot')
        seen = set()
        for entry in bundle['entries']:
            eid = entry['id']; day = entry['local_date']
            if not re.fullmatch(r'[a-f0-9]{32}', eid) or eid in seen or type(entry['version']) is not int or entry['version'] < 1:
                raise ValueError('Invalid entry ID or version')
            seen.add(eid)
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError('Invalid local date')
            audio = entry.get('audio')
            if audio and audio['included']:
                relative = 'audio/' + eid + '.ogg'
                if audio['path'] != relative or checksums.get(relative) != audio['sha256']:
                    raise ValueError('Audio metadata mismatch')
                destination = vault / 'Attachments' / 'hourly-diary' / (eid + '.ogg')
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_symlink(): raise ValueError('Attachment symlink forbidden')
                part = destination.with_suffix('.part'); shutil.copyfile(stage / relative, part); part.replace(destination)
        changed = 0; previous_dates = previous_receipt.get('local_dates', {})
        affected = {e['local_date'] for e in bundle['entries']}
        for entry in bundle['entries']:
            previous = previous_dates.get(entry['id'])
            if previous and previous != entry['local_date']:
                if date.fromisoformat(previous).isoformat() != previous: raise ValueError('Invalid previous date')
                affected.add(previous)
        for day in sorted(affected):
            body = render_markdown([e for e in bundle['entries'] if e['local_date'] == day], bundle['timezone'], '../Attachments/hourly-diary/')
            moved = {e['id']: e['version'] for e in bundle['entries'] if previous_dates.get(e['id']) == day and e['local_date'] != day}
            changed += atomic_note(vault / 'Daily' / (day + '.md'), body, moved)
        receipt = {'schema_version': 1, 'generated_at': bundle['generated_at'],
                   'versions': {**previous_versions, **{e['id']: e['version'] for e in bundle['entries']}},
                   'local_dates': {**previous_dates, **{e['id']: e['local_date'] for e in bundle['entries']}}}
        part = vault / '.hourly-diary-receipt.part'; part.write_text(json.dumps(receipt, indent=2), encoding='utf-8'); part.replace(vault / '.hourly-diary-receipt.json')
        return {'result': 'ok', 'entries': len(seen), 'days_changed': changed, 'complete': bundle.get('complete', False)}


def _ssh_options():
    return ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15']


def _remote_command(remote_run_as, command):
    if remote_run_as and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', remote_run_as): raise ValueError('Invalid remote service user')
    prefix = ['runuser', '-u', remote_run_as, '--'] if remote_run_as else []
    return shlex.join(prefix + command)


def pull(host, vault, remote_python='/opt/hourly-diary/venv/bin/python', remote_data='/var/lib/hourly-diary', ssh_port=8443, remote_run_as='hourlydiary'):
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+', host) or host.startswith('-'): raise ValueError('Invalid SSH host')
    remote_archive = remote_data.rstrip('/') + '/sync/latest.zip'
    command = _remote_command(remote_run_as, [remote_python, '-m', 'hourly_diary', 'snapshot', '--data-dir', remote_data, '--output', remote_archive])
    with tempfile.TemporaryDirectory(prefix='diary-pull-') as directory:
        local = Path(directory) / 'snapshot.zip'; options = _ssh_options()
        subprocess.run(['ssh', *options, '-p', str(ssh_port), host, command], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        subprocess.run(['scp', *options, '-P', str(ssh_port), host + ':' + remote_archive, str(local)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        return import_bundle(local, vault)


def sync_once(host, vault, remote_python='/opt/hourly-diary/venv/bin/python', remote_data='/var/lib/hourly-diary', ssh_port=8443, remote_run_as='hourlydiary'):
    """Consume exactly one explicit owner request; idle means no download."""
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+', host) or host.startswith('-'): raise ValueError('Invalid SSH host')
    options = _ssh_options()
    claim_cmd = _remote_command(remote_run_as, [remote_python, '-m', 'hourly_diary', 'sync-claim', '--data-dir', remote_data])
    raw = subprocess.check_output(['ssh', *options, '-p', str(ssh_port), host, claim_cmd], stderr=subprocess.DEVNULL, timeout=60, text=True)
    try: claim = json.loads(raw)
    except (ValueError, TypeError): raise ValueError('Invalid sync claim response') from None
    if claim.get('result') == 'idle': return {'result': 'idle'}
    request_id = claim.get('request_id')
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id): raise ValueError('Invalid sync request ID')
    remote_archive = remote_data.rstrip('/') + '/sync/requests/' + request_id + '.zip'
    finish_state, finish_code = 'error', 'local_sync_error'
    try:
        snapshot_cmd = _remote_command(remote_run_as, [remote_python, '-m', 'hourly_diary', 'snapshot', '--data-dir', remote_data, '--output', remote_archive])
        subprocess.run(['ssh', *options, '-p', str(ssh_port), host, snapshot_cmd], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        with tempfile.TemporaryDirectory(prefix='diary-explicit-sync-') as directory:
            local = Path(directory) / (request_id + '.zip')
            subprocess.run(['scp', *options, '-P', str(ssh_port), host + ':' + remote_archive, str(local)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            result = import_bundle(local, vault)
        finish_state, finish_code = 'completed', None
        return {'result': 'completed', 'request_id': request_id, **result}
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile):
        return {'result': 'error', 'request_id': request_id, 'code': finish_code}
    finally:
        finish_cmd = _remote_command(remote_run_as, [remote_python, '-m', 'hourly_diary', 'sync-finish', '--data-dir', remote_data, '--request-id', request_id, '--state', finish_state, '--error-code', finish_code or ''])
        try: subprocess.run(['ssh', *options, '-p', str(ssh_port), host, finish_cmd], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.SubprocessError): pass
