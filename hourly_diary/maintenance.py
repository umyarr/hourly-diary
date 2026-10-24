"""Consistent SQLite backups with shared audio objects; offline safe restore."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid
from .exporting import audio_source, file_sha
from .store import Store


def integrity(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity failed')
    finally:
        con.close()


def backup(data_dir, output, keep=14):
    data_dir, output = Path(data_dir), Path(output)
    if keep < 1:
        raise ValueError('Retention must be positive')
    output.mkdir(parents=True, exist_ok=True)
    objects = output / 'objects'
    objects.mkdir(exist_ok=True)
    generation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix='.pending-', dir=output) as tmp:
        tmp = Path(tmp)
        store = Store(data_dir / 'diary.sqlite3')
        try:
            store.backup_to(tmp / 'diary.sqlite3')
        finally:
            store.close()
        con = sqlite3.connect((tmp / 'diary.sqlite3').as_uri() + '?mode=ro', uri=True)
        try:
            refs = con.execute('SELECT audio_path,audio_sha256 FROM entries WHERE audio_path IS NOT NULL').fetchall()
        finally:
            con.close()
        manifest = {'schema_version': 1, 'database_sha256': file_sha(tmp / 'diary.sqlite3'), 'audio': {}}
        for relative, digest in refs:
            source = audio_source(data_dir, relative)
            if not re.fullmatch(r'[a-f0-9]{64}', digest or '') or file_sha(source) != digest:
                raise ValueError('Audio integrity failed')
            target = objects / digest
            if not target.exists():
                part = objects / (digest + '.part')
                shutil.copyfile(source, part)
                part.replace(target)
            elif target.is_symlink() or file_sha(target) != digest:
                raise ValueError('Backup object integrity failed')
            manifest['audio'][relative] = digest
        (tmp / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        destination = output / generation
        # Publish only a fully complete generation, atomically on this filesystem.
        tmp.rename(destination)
    generations = sorted(p for p in output.iterdir() if p.is_dir() and re.fullmatch(r'\d{8}T\d{6}-[a-f0-9]{8}', p.name))
    for old in generations[:-keep]:
        shutil.rmtree(old)
    # Shared objects are intentionally not garbage-collected automatically.
    # Off-host copy must include objects AND generation directories.
    return destination


def restore(generation, target):
    generation, target = Path(generation), Path(target)
    if target.exists():
        raise ValueError('Restore target must not exist; stop service before switching data directories')
    if generation.is_symlink() or (generation / 'diary.sqlite3').is_symlink() or (generation / 'manifest.json').is_symlink():
        raise ValueError('Backup symlinks forbidden')
    manifest = json.loads((generation / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or not isinstance(manifest.get('audio'), dict):
        raise ValueError('Unsupported backup')
    database = generation / 'diary.sqlite3'
    if file_sha(database) != manifest['database_sha256']:
        raise ValueError('Database checksum failed')
    integrity(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.restore-', dir=target.parent) as tmp:
        tmp = Path(tmp)
        shutil.copyfile(database, tmp / 'diary.sqlite3')
        for relative, digest in manifest['audio'].items():
            if not re.fullmatch(r'audio/[a-f0-9]{32}\.ogg', relative) or not re.fullmatch(r'[a-f0-9]{64}', digest):
                raise ValueError('Unsafe backup path')
            source = generation.parent / 'objects' / digest
            if source.is_symlink() or file_sha(source) != digest:
                raise ValueError('Audio checksum failed')
            dest = tmp / relative
            dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, dest)
        # Ensure metadata did not omit an audio reference from the database.
        con = sqlite3.connect((tmp / 'diary.sqlite3').as_uri() + '?mode=ro', uri=True)
        try:
            refs = dict(con.execute('SELECT audio_path,audio_sha256 FROM entries WHERE audio_path IS NOT NULL'))
        finally:
            con.close()
        if refs != manifest['audio']:
            raise ValueError('Backup audio references differ')
        tmp.rename(target)
