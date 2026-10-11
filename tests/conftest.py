import os
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from mixsync.core.config import Settings

_ENV = "MIXSYNC_TEST_DATABASE_URL"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--db", choices=["sqlite", "postgres"], default="sqlite")


def _postgres_url(config: pytest.Config) -> str | None:
    if config.getoption("--db") != "postgres":
        return None
    url = os.environ.get(_ENV)
    if not url:
        raise pytest.UsageError(f"--db=postgres needs {_ENV}, e.g. postgresql+psycopg://...")
    check_test_db_url(url)
    return url


def check_test_db_url(url: str) -> None:
    """Tests create and drop schemas: refuse anything that might be a real database."""
    u = make_url(url)
    if overrides := {"dbname", "service", "options"} & set(u.query):
        # libpq takes these over the URL path, so the name check below would be bypassed
        raise pytest.UsageError(f"{_ENV} must not set {', '.join(sorted(overrides))}")
    name = u.database or ""
    if not (name.startswith("test_") or name.endswith("_test")):
        raise pytest.UsageError(f"{_ENV} database {name!r} must start test_ or end _test")


def pytest_configure(config: pytest.Config) -> None:
    _postgres_url(config)  # fail fast, before any test runs


@pytest.fixture(autouse=True)
def _block_sockets(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    def blocked(*_a: object, **_k: object) -> None:
        raise RuntimeError("network access is blocked in unit tests")

    # psycopg connects through libpq in C, so Postgres needs no exception here
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Settings]:
    pg = _postgres_url(request.config)
    if not pg:
        monkeypatch.setenv("MIXSYNC_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
        yield Settings()
        return
    # One fresh schema per test (unique across xdist workers), selected via search_path.
    schema = f"t_{uuid.uuid4().hex}"
    admin = create_engine(pg, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(pg).update_query_dict(
        {"options": f"-csearch_path={schema}", "application_name": schema}
    )
    monkeypatch.setenv("MIXSYNC_DATABASE_URL", url.render_as_string(hide_password=False))
    try:
        yield Settings()
    finally:
        with admin.connect() as c:
            # tests never dispose their engines; free the connections so the server isn't exhausted
            c.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE application_name = :n"
                ),
                {"n": schema},
            )
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
