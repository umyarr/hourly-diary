"""Application and offline maintenance CLI. Does not load env files implicitly."""
import argparse
import json
import os
from pathlib import Path
import sys
from .config import Config
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m hourly_diary')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('run')
    sub.add_parser('commands')
    p = sub.add_parser('snapshot')
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('backup')
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--keep', type=int, default=14)
    p = sub.add_parser('restore')
    p.add_argument('--generation', type=Path, required=True)
    p.add_argument('--target', type=Path, required=True)
    p = sub.add_parser('import')
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--vault', type=Path, required=True)
    p = sub.add_parser('pull')
    p.add_argument('--host', required=True)
    p.add_argument('--vault', type=Path, required=True)
    p.add_argument('--port', type=int, default=8443)
    p.add_argument('--remote-python', default='/opt/hourly-diary/venv/bin/python')
    p.add_argument('--remote-data', default='/var/lib/hourly-diary')
    p.add_argument('--remote-run-as', default='hourlydiary', help='Service user via runuser; empty for same-user SSH')
    p = sub.add_parser('sync-poll')
    p.add_argument('--host', required=True)
    p.add_argument('--vault', type=Path, required=True)
    p.add_argument('--port', type=int, default=8443)
    p.add_argument('--remote-python', default='/opt/hourly-diary/venv/bin/python')
    p.add_argument('--remote-data', default='/var/lib/hourly-diary')
    p.add_argument('--remote-run-as', default='hourlydiary')
    sub.add_parser('sync-claim').add_argument('--data-dir', type=Path, required=True)
    p = sub.add_parser('sync-finish')
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--request-id', required=True)
    p.add_argument('--state', choices=['completed', 'error'], required=True)
    p.add_argument('--error-code', default='')
    p = sub.add_parser('pull-backup')
    p.add_argument('--host', required=True)
    p.add_argument('--destination', type=Path, required=True)
    p.add_argument('--port', type=int, default=8443)
    p.add_argument('--remote-python', default='/opt/hourly-diary/venv/bin/python')
    p.add_argument('--remote-data', default='/var/lib/hourly-diary')
    p.add_argument('--remote-run-as', default='hourlydiary')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == 'run':
            from .runtime import run
            run(Config.from_env())
        elif args.command == 'commands':
            from .commands import COMMANDS
            print(json.dumps([{'command': n, 'description': d} for n, d in COMMANDS], ensure_ascii=True))
        elif args.command == 'snapshot':
            from .exporting import build_export
            from .runtime import process_lock
            if not (args.data_dir / 'diary.sqlite3').is_file():
                raise ValueError('Database missing')
            if args.output.resolve().is_relative_to((args.data_dir / 'audio').resolve()) or args.output.resolve() == (args.data_dir / 'diary.sqlite3').resolve():
                raise ValueError('Unsafe snapshot destination')
            with process_lock(args.data_dir / 'snapshot.lock'):
                store = Store(args.data_dir / 'diary.sqlite3')
                try:
                    config = Config('', 1, data_dir=args.data_dir, timezone=store.get_setting('timezone', 'Europe/Moscow'))
                    bundle = build_export(store, config, args.output)
                    print(json.dumps({'result': 'ok', 'complete': bundle['complete'], 'entries': len(bundle['entries'])}))
                finally:
                    store.close()
        elif args.command == 'backup':
            from .maintenance import backup
            if not (args.data_dir / 'diary.sqlite3').is_file():
                raise ValueError('Database missing')
            from .runtime import process_lock
            with process_lock(args.output / '.backup.lock'):
                generation = backup(args.data_dir, args.output, args.keep)
            print(json.dumps({'result': 'ok', 'generation': generation.name}))
        elif args.command == 'restore':
            from .maintenance import restore
            restore(args.generation, args.target)
            print('{"result":"ok"}')
        elif args.command == 'import':
            from .sync import import_bundle
            print(json.dumps(import_bundle(args.archive, args.vault)))
        elif args.command == 'sync-poll':
            from .sync import sync_once
            print(json.dumps(sync_once(args.host, args.vault, args.remote_python, args.remote_data,
                                       args.port, args.remote_run_as)))
        elif args.command == 'sync-claim':
            from datetime import datetime, timezone
            store = Store(args.data_dir / 'diary.sqlite3')
            try:
                request_id = store.claim_sync(datetime.now(timezone.utc).isoformat())
                print(json.dumps({'result': 'claimed', 'request_id': request_id} if request_id else {'result': 'idle'}))
            finally:
                store.close()
        elif args.command == 'sync-finish':
            import re
            if not re.fullmatch(r'[a-f0-9]{32}', args.request_id):
                raise ValueError('Invalid request ID')
            store = Store(args.data_dir / 'diary.sqlite3')
            try:
                from datetime import datetime, timezone
                ok = store.finish_sync(args.request_id, args.state, datetime.now(timezone.utc).isoformat(), args.error_code or None)
                archive = args.data_dir / 'sync' / 'requests' / (args.request_id + '.zip')
                archive.unlink(missing_ok=True)
                print(json.dumps({'result': 'ok' if ok else 'not_found'}))
            finally:
                store.close()
        elif args.command == 'pull-backup':
            from .offhost import pull_backup
            print(json.dumps(pull_backup(args.host, args.destination, args.port,
                             args.remote_python, args.remote_data, args.remote_run_as)))
        elif args.command == 'pull':
            from .sync import pull
            print(json.dumps(pull(args.host, args.vault, args.remote_python, args.remote_data, args.port, args.remote_run_as)))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        # No traceback, env values, HTTP URLs or content on operational errors.
        print(json.dumps({'result': 'error', 'code': args.command + '_failed'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
