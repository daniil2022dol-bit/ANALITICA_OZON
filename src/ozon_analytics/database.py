from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import settings


@contextmanager
def connect(web=False):
    with psycopg.connect(settings.web_database_url if web else settings.database_url,
                         row_factory=dict_row, connect_timeout=10) as conn:
        yield conn


def migrate():
    with connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(95141801)')
        conn.execute('CREATE TABLE IF NOT EXISTS public.schema_migration (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
        applied = {r['name'] for r in conn.execute('SELECT name FROM public.schema_migration')}
        for path in sorted(Path('db').glob('[0-9][0-9][0-9]_*.sql')):
            if path.name not in applied:
                # Migration files already contain BEGIN/COMMIT; remove those
                # boundaries to keep SQL and migration ledger atomic together.
                sql = '\n'.join(line for line in path.read_text().splitlines()
                                if line.strip().upper() not in ('BEGIN;', 'COMMIT;'))
                conn.execute(sql)
                conn.execute('INSERT INTO public.schema_migration(name) VALUES(%s)', (path.name,))
