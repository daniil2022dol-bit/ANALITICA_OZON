"""Run as root: provision private native PostgreSQL roles from protected env."""

import subprocess
from urllib.parse import unquote, urlparse

from dotenv import dotenv_values
from psycopg import sql


def execute(statement):
    result = subprocess.run(
        [
            "runuser",
            "-u",
            "postgres",
            "--",
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            "postgres",
        ],
        input=statement.as_string() if hasattr(statement, "as_string") else statement,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("Database provisioning failed; credentials suppressed")
    return result.stdout.strip()


values = dotenv_values("/etc/ozon-analytics/service.env")
for key in ("DATABASE_URL", "WEB_DATABASE_URL"):
    url = urlparse(values[key])
    role, password = unquote(url.username), unquote(url.password)
    if not execute(
        sql.SQL("SELECT 1 FROM pg_roles WHERE rolname={}").format(sql.Literal(role))
    ):
        execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    execute(
        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )
    if key == "DATABASE_URL":
        database = url.path.lstrip("/")
        if not execute(
            sql.SQL("SELECT 1 FROM pg_database WHERE datname={}").format(
                sql.Literal(database)
            )
        ):
            execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
