import importlib.util
from pathlib import Path

import pytest

# the root conftest by path: `import conftest` would find tests/unit/conftest.py first
_spec = importlib.util.spec_from_file_location(
    "root_conftest", Path(__file__).parents[1] / "conftest.py"
)
assert _spec and _spec.loader
_root = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_root)
check_test_db_url = _root.check_test_db_url

BASE = "postgresql+psycopg://u:p@localhost:5432/"


@pytest.mark.parametrize("name", ["mixsync_test", "test_mixsync"])
def test_test_databases_pass(name: str) -> None:
    check_test_db_url(BASE + name)


@pytest.mark.parametrize(
    "url",
    [
        BASE + "prod",
        BASE + "latest",  # contains "test" by accident
        BASE + "contest_db",
        BASE.rstrip("/"),  # no database name
        BASE + "mixsync_test?dbname=prod",  # libpq would connect to prod
        BASE + "mixsync_test?service=prod",
        BASE + "mixsync_test?options=-csearch_path%3Dpublic",
    ],
)
def test_anything_else_is_refused(url: str) -> None:
    with pytest.raises(pytest.UsageError):
        check_test_db_url(url)
