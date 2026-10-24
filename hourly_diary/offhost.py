"""Private off-host backup pull using existing OpenSSH credentials."""
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from .maintenance import restore
from .runtime import process_lock


def pull_backup(host, destination, port=8443, remote_python='/opt/hourly-diary/venv/bin/python',
                remote_data='/var/lib/hourly-diary', remote_run_as='hourlydiary'):
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+', host) or host.startswith('-'):
        raise ValueError('Invalid host')
    if remote_run_as and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', remote_run_as):
        raise ValueError('Invalid remote user')
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    options = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15']
    prefix = ['runuser', '-u', remote_run_as, '--'] if remote_run_as else []
    backup_root = remote_data.rstrip('/') + '/backups'
    command = shlex.join(prefix + [remote_python, '-m', 'hourly_diary', 'backup',
                                  '--data-dir', remote_data, '--output', backup_root, '--keep', '14'])
    with process_lock(destination.parent / '.backup-pull.lock'), tempfile.TemporaryDirectory(prefix='.backup-pull-', dir=destination.parent) as tmp:
        tmp = Path(tmp)
        subprocess.run(['ssh', *options, '-p', str(port), host, command], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        subprocess.run(['scp', *options, '-P', str(port), '-r', host + ':' + backup_root, str(tmp / 'copy')],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
        incoming = tmp / 'copy'
        generations = sorted(p for p in incoming.iterdir() if p.is_dir() and re.fullmatch(r'\d{8}T\d{6}-[a-f0-9]{8}', p.name))
        if not generations:
            raise ValueError('No backup generations')
        # Check every generation, including audio refs; temporary restored copies
        # are removed without ever starting the bot or copying env secrets.
        for index, generation in enumerate(generations):
            check_dir = tmp / ('restore-' + str(index))
            restore(generation, check_dir)
            shutil.rmtree(check_dir)
        receipt = {'result': 'ok', 'generations': len(generations), 'latest': generations[-1].name,
                   'restore_checked': True}
        (incoming / 'OFFHOST_RECEIPT.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        previous = destination.with_name(destination.name + '.previous')
        if previous.exists():
            shutil.rmtree(previous)
        if destination.exists():
            destination.rename(previous)
        try:
            incoming.rename(destination)
        except OSError:
            if previous.exists() and not destination.exists():
                previous.rename(destination)
            raise
        return receipt
