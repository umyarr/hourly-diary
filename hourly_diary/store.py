"""Serialized SQLite access. Public operations are transactional and thread-safe."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading

SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inbox(update_id INTEGER PRIMARY KEY, body TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS prompts(id TEXT PRIMARY KEY, start TEXT NOT NULL,
    end TEXT NOT NULL UNIQUE, day TEXT NOT NULL, state TEXT NOT NULL,
    message_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY, message_id INTEGER UNIQUE NOT NULL,
    prompt_id TEXT REFERENCES prompts(id), parent_id TEXT REFERENCES entries(id), reply_message_id INTEGER,
    day TEXT NOT NULL, message_at TEXT NOT NULL, received_at TEXT NOT NULL,
    kind TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, original_text TEXT,
    text TEXT, corrected TEXT, edited_at TEXT, file_id TEXT, file_unique_id TEXT,
    audio_path TEXT, audio_sha256 TEXT, audio_size INTEGER, duration INTEGER, mime_type TEXT,
    status TEXT NOT NULL DEFAULT 'saved', transcript TEXT, error_code TEXT,
    original_unknown INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS revisions(entry_id TEXT NOT NULL REFERENCES entries(id),
    version INTEGER NOT NULL, update_id INTEGER NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(entry_id,version));
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
    entry_id TEXT, payload TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS media_job ON jobs(kind,entry_id) WHERE entry_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS responses(update_id INTEGER PRIMARY KEY,
    text TEXT NOT NULL, export_start TEXT, export_end TEXT, state TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS sync_requests(id TEXT PRIMARY KEY, requested_at TEXT NOT NULL,
    state TEXT NOT NULL, claimed_at TEXT, finished_at TEXT, error_code TEXT);
PRAGMA user_version=1;
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError('Unsupported database schema')
        self.db.executescript(SCHEMA)
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            with self.db:
                yield self.db

    def close(self):
        with self.lock:
            self.db.close()

    def recover(self):
        with self.transaction() as c:
            c.execute("UPDATE prompts SET state='unknown' WHERE state='sending'")
            c.execute("UPDATE jobs SET state='pending' WHERE state='running'")
            # A response might have reached Telegram: do not blindly resend.
            c.execute("UPDATE responses SET state='unknown' WHERE state='sending'")

    @staticmethod
    def setting(c, key, default=None):
        row = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def set_setting(c, key, value):
        c.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  (key, json.dumps(value)))

    def get_setting(self, key, default=None):
        with self.lock:
            return self.setting(self.db, key, default)

    def enqueue(self, update):
        with self.transaction() as c:
            uid = int(update['update_id'])
            c.execute('INSERT OR IGNORE INTO inbox(update_id,body) VALUES (?,?)',
                      (uid, json.dumps(update, ensure_ascii=True)))
            self.set_setting(c, 'offset', max(uid + 1, self.setting(c, 'offset', 0)))

    def pending_updates(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM inbox WHERE done=0 ORDER BY update_id LIMIT 100')]

    def entries(self, start, end):
        with self.lock:
            result = []
            for row in self.db.execute('''SELECT e.*, p.start period_start,p.end period_end
                FROM entries e LEFT JOIN prompts p ON p.id=e.prompt_id
                WHERE e.day>=? AND e.day<=? ORDER BY e.day,COALESCE(p.start,e.message_at),e.message_at,e.id''', (start, end)):
                item = dict(row)
                item['revisions'] = [json.loads(r[0]) for r in self.db.execute(
                    'SELECT body FROM revisions WHERE entry_id=? ORDER BY version', (item['id'],))]
                result.append(item)
            return result

    def media_update(self, entry_id, **values):
        allowed = {'audio_path', 'audio_sha256', 'audio_size', 'status', 'transcript', 'error_code'}
        if not values or not set(values) <= allowed:
            raise ValueError('Invalid media fields')
        with self.transaction() as c:
            c.execute('UPDATE entries SET ' + ','.join(k + '=?' for k in values)
                      + ',version=version+1 WHERE id=?', (*values.values(), entry_id))

    def job(self, now):
        with self.transaction() as c:
            row = c.execute("SELECT * FROM jobs WHERE state='pending' AND retry_at<=? ORDER BY id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?", (row['id'],))
            result = dict(row)
            result['attempts'] += 1
            return result

    def finish_job(self, job_id, state='done', retry_at=0):
        with self.transaction() as c:
            c.execute('UPDATE jobs SET state=?,retry_at=? WHERE id=?', (state, retry_at, job_id))

    def get_entry(self, entry_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone()
            return dict(row) if row else None

    def response(self):
        with self.transaction() as c:
            row = c.execute("SELECT * FROM responses WHERE state='pending' ORDER BY update_id LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE responses SET state='sending' WHERE update_id=?", (row['update_id'],))
                return dict(row)

    def finish_response(self, update_id, state):
        with self.transaction() as c:
            c.execute('UPDATE responses SET state=? WHERE update_id=?', (state, update_id))

    def request_sync(self, requested_at):
        with self.transaction() as c:
            active = c.execute("SELECT id,state FROM sync_requests WHERE state IN ('requested','claimed','running') ORDER BY requested_at DESC LIMIT 1").fetchone()
            if active:
                return dict(active), False
            import uuid
            request_id = uuid.uuid4().hex
            c.execute("INSERT INTO sync_requests(id,requested_at,state) VALUES (?,?, 'requested')",
                      (request_id, requested_at))
            return {'id': request_id, 'state': 'requested'}, True

    def claim_sync(self, claimed_at):
        with self.transaction() as c:
            row = c.execute("SELECT id FROM sync_requests WHERE state='requested' ORDER BY requested_at LIMIT 1").fetchone()
            if not row:
                return None
            cursor = c.execute("UPDATE sync_requests SET state='running',claimed_at=? WHERE id=? AND state='requested'",
                               (claimed_at, row['id']))
            return row['id'] if cursor.rowcount else None

    def finish_sync(self, request_id, state, finished_at, error_code=None):
        if state not in ('completed', 'error'):
            raise ValueError('Invalid sync state')
        with self.transaction() as c:
            cursor = c.execute("UPDATE sync_requests SET state=?,finished_at=?,error_code=? WHERE id=? AND state='running'",
                               (state, finished_at, error_code, request_id))
            return cursor.rowcount == 1

    def latest_sync(self):
        with self.lock:
            row = self.db.execute('SELECT * FROM sync_requests ORDER BY requested_at DESC LIMIT 1').fetchone()
            return dict(row) if row else None

    def backup_to(self, destination):
        with self.lock:
            output = sqlite3.connect(destination)
            try:
                self.db.backup(output)
                if output.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('Backup integrity failed')
            finally:
                output.close()
