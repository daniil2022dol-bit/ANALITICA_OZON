"""Root-only restoration drill into a new disposable PostgreSQL database.

Usage: PYTHONPATH=src .venv/bin/python ops/verify_restore.py backup.tar.gz
Only the uniquely created drill database is removed, including on failure.
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from pwd import getpwnam

from ozon_analytics.operations import check_backup


def run(arguments, **kwargs):
    return subprocess.run(
        ["runuser", "-u", "postgres", "--", *arguments],
        check=True,
        capture_output=True,
        text=True,
        **kwargs,
    )


def verify(path):
    if os.geteuid() != 0:
        raise RuntimeError("Run this isolated PostgreSQL drill as root")
    check_backup(path)
    database = "ozon_restore_drill_" + uuid.uuid4().hex[:12]
    postgres = getpwnam("postgres")
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix="ozon-restore-") as temporary:
            os.chown(temporary, postgres.pw_uid, postgres.pw_gid)
            dump = Path(temporary) / "database.dump"
            with tarfile.open(path, "r:gz") as archive, dump.open("wb") as output:
                shutil.copyfileobj(archive.extractfile("database.dump"), output)
            dump.chmod(0o600)
            os.chown(dump, postgres.pw_uid, postgres.pw_gid)
            run(["createdb", database])
            created = True
            run(
                [
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "--dbname",
                    database,
                    str(dump),
                ]
            )
            query = """SELECT json_build_object(
                'stock_rows',(SELECT count(*) FROM ozon.v_stock_daily),
                'postings',(SELECT count(*) FROM ozon.posting),
                'storage_rows',(SELECT count(*) FROM ozon.storage_row),
                'products',(SELECT count(*) FROM ozon.product))"""
            result = run(
                [
                    "psql",
                    "-X",
                    "-qAt",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-d",
                    database,
                    "-c",
                    query,
                ]
            )
            counts = json.loads(result.stdout)
            print("Restored database is readable:", json.dumps(counts))
            return counts
    finally:
        if created:
            run(["dropdb", database])


if __name__ == "__main__":
    verify(sys.argv[1])
