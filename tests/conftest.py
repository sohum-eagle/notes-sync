import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DSN = os.environ.get("FOLDER_ROUTING_TEST_DATABASE_URL", "")


@pytest.fixture
def dsn():
    if not TEST_DSN:
        pytest.skip("set FOLDER_ROUTING_TEST_DATABASE_URL to a disposable Postgres database")
    return TEST_DSN


@pytest.fixture
def store(dsn):
    import psycopg
    from folder_routing.store import Store
    schema = "t_" + uuid.uuid4().hex[:12]
    st = Store(dsn, schema)
    st.migrate()
    yield st
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.fixture(autouse=True)
def _reset_runtime():
    from folder_routing import runtime
    runtime.configure()
    yield
    runtime.configure()
