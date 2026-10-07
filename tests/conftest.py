import os
from urllib.parse import urlparse

import pytest

# Integration tests must target an explicitly provided disposable database.
TEST_URL = os.environ.get("TEST_DATABASE_URL")
os.environ.update(
    OZON_CLIENT_ID="123",
    OZON_API_KEY="test-only",
    ADMIN_PASSWORD="test-password",
    SESSION_SECRET="x" * 48,
)
if TEST_URL:
    os.environ["DATABASE_URL"] = TEST_URL
    os.environ["WEB_DATABASE_URL"] = TEST_URL


@pytest.fixture
def db():
    if not TEST_URL or not urlparse(TEST_URL).path.lstrip("/").startswith("ozon_test"):
        pytest.skip("Provide a disposable TEST_DATABASE_URL containing ozon_test")
    from ozon_analytics.database import connect, migrate

    with connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS ozon CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.schema_migration")
    migrate()
    migrate()  # Migration ledger must make repeated deployment safe.
    with connect() as conn:
        conn.execute(
            "INSERT INTO ozon.seller_account(client_id,name) VALUES(123,'Test')"
        )
    return connect
