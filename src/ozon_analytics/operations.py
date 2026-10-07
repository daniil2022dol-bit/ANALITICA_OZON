import hashlib
import json
import os
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import psutil

from .config import settings
from .database import connect


def monitor():
    memory,disk,swap = psutil.virtual_memory(),psutil.disk_usage('/'),psutil.swap_memory()
    with connect() as conn:
        conn.execute('INSERT INTO ozon.host_metric(cpu_percent,memory_percent,memory_available_bytes,disk_percent,disk_free_bytes,swap_percent,load_1) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                     (psutil.cpu_percent(interval=1),memory.percent,memory.available,disk.percent,disk.free,swap.percent,os.getloadavg()[0]))
        conn.execute("DELETE FROM ozon.host_metric WHERE measured_at<now()-interval '90 days'")


def pg_environment():
    p=urlparse(settings.database_url)
    return {**os.environ,'PGHOST':p.hostname or '127.0.0.1','PGPORT':str(p.port or 5432),
            'PGUSER':unquote(p.username or ''),'PGPASSWORD':unquote(p.password or ''),
            'PGDATABASE':p.path.lstrip('/')}


def backup():
    directory=Path(os.getenv('BACKUP_DIR','var/backups'))
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    name='ozon-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    dump=directory/(name+'.dump.partial')
    partial=directory/(name+'.tar.gz.partial')
    target=directory/(name+'.tar.gz')
    with connect() as conn:
        backup_id=conn.execute('INSERT INTO ozon.backup_run DEFAULT VALUES RETURNING id').fetchone()['id']
    try:
        subprocess.run(['pg_dump','--format=custom','--no-owner','--no-acl','--file',str(dump)],
                       env=pg_environment(),check=True,capture_output=True)
        dump.chmod(0o600)
        subprocess.run(['pg_restore','--list',str(dump)],check=True,capture_output=True)
        manifest={'database_file':'database.dump','database_sha256':hashlib.sha256(dump.read_bytes()).hexdigest(),
                  'created_at':datetime.now(timezone.utc).isoformat(),'format_version':1}
        manifest_path=directory/(name+'.manifest.json')
        manifest_path.write_text(json.dumps(manifest));manifest_path.chmod(0o600)
        with tarfile.open(partial,'w:gz') as archive:
            archive.add(dump,arcname='database.dump')
            archive.add(manifest_path,arcname='manifest.json')
            reports=settings.data_dir/'reports'
            if reports.exists():
                archive.add(reports,arcname='reports')
        partial.chmod(0o600);partial.replace(target)
        digest=hashlib.sha256(target.read_bytes()).hexdigest()
        remote=os.getenv('BACKUP_REMOTE','')
        offsite=False
        if remote:
            command=['rclone','copyto',str(target),remote.rstrip('/')+'/'+target.name]
            config=os.getenv('RCLONE_CONFIG')
            if config:
                command+=['--config',config]
            subprocess.run(command,check=True,capture_output=True)
            offsite=True
        with connect() as conn:
            conn.execute("UPDATE ozon.backup_run SET status='success',finished_at=now(),local_path=%s,sha256=%s,size_bytes=%s,offsite=%s WHERE id=%s",
                         (str(target),digest,target.stat().st_size,offsite,backup_id))
        # Rotation begins only after a successfully verified new archive.
        for old in sorted(directory.glob('ozon-*.tar.gz'),reverse=True)[8:]:
            old.unlink()
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE ozon.backup_run SET status='failed',finished_at=now(),error=%s WHERE id=%s",(type(exc).__name__,backup_id))
        raise
    finally:
        for f in (dump,partial,directory/(name+'.manifest.json')):
            f.unlink(missing_ok=True)


def check_backup(path):
    with tarfile.open(path,'r:gz') as archive:
        # No extraction of arbitrary paths from an untrusted tarball.
        manifest=json.load(archive.extractfile('manifest.json'))
        data=archive.extractfile('database.dump').read()
        if hashlib.sha256(data).hexdigest()!=manifest['database_sha256']:
            raise ValueError('Backup checksum mismatch')
    return manifest
