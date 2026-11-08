import argparse
import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from mixsync.core.config import Settings

COMMANDS = ("web", "worker", "all", "migrate", "backup", "user-add", "worker-health")


def configure_logging(settings: "Settings | None" = None) -> str:
    """Root logging for the long-running processes, at the log level (MIXSYNC_LOG_LEVEL unless
    `settings` is given). Returns the level. uvicorn's loggers keep their own handlers and don't
    propagate, so nothing prints twice."""
    from mixsync.core import config

    level = (settings or config.Settings()).log_level  # an invalid level fails before startup
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    return level


def startup_settings() -> "tuple[Settings, datetime | None]":
    """The snapshot web, worker and `all` build services from (ADR 0021), and its settings
    version, logged at its level. Logging starts at the env level so an invalid saved setting
    is reported while resolving."""
    from mixsync.db.app_settings import load_settings

    configure_logging()
    settings, version = load_settings()
    configure_logging(settings)
    return settings, version


def worker_health() -> int:
    """0 when a worker recorded it was alive within WORKER_STALE_AFTER: the compose healthcheck."""
    from mixsync.core.clock import SystemClock
    from mixsync.core.jobs import WORKER_STALE_AFTER
    from mixsync.db.engine import make_engine, make_session_factory
    from mixsync.db.queue import worker_age

    with make_session_factory(make_engine())() as s:
        age = worker_age(s, SystemClock().now())
    if age is None:
        print("no worker has recorded a heartbeat", file=sys.stderr)
        return 1
    if age > WORKER_STALE_AFTER:
        print(f"worker last seen {age.total_seconds():.0f}s ago", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mixsync")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("username", nargs="?", help="user-add only")
    parser.add_argument("--admin", action="store_true", help="user-add only")
    args = parser.parse_args(argv)
    cmd: str = args.command
    if cmd == "web":
        import uvicorn

        from mixsync.app import create_app
        from mixsync.wiring import build_services

        # built here, not by uvicorn: a bad key file must stop startup (build_services loads it)
        settings, version = startup_settings()
        uvicorn.run(
            create_app(services=build_services(settings, settings_version=version)),
            host="0.0.0.0",
            port=8080,
            log_level=settings.log_level.lower(),
        )
    elif cmd == "migrate":
        from mixsync.db.migrate import upgrade

        upgrade()
    elif cmd == "backup":
        from pathlib import Path

        from mixsync.core.clock import SystemClock
        from mixsync.core.config import Settings
        from mixsync.db.backup import backup
        from mixsync.db.engine import make_engine

        settings = Settings()
        try:
            path = backup(
                make_engine(settings),
                settings.backup_dir or Path(settings.config_dir) / "backups",
                SystemClock(),
            )
        except Exception as e:
            print(f"backup failed: {e}", file=sys.stderr)
            return 1
        print(path)
    elif cmd == "user-add":
        if not args.username:
            parser.error("user-add requires a username")
        from getpass import getpass

        from mixsync.db.auth import create_user
        from mixsync.db.engine import make_engine, make_session_factory

        password = getpass("Password: ")
        if not password or password != getpass("Repeat password: "):
            print("passwords empty or do not match", file=sys.stderr)
            return 1
        with make_session_factory(make_engine())() as s, s.begin():
            create_user(s, args.username, password, admin=args.admin)
    elif cmd in ("worker", "all"):
        import asyncio

        from mixsync.worker import serve_all, serve_worker

        settings, version = startup_settings()
        serve = serve_worker if cmd == "worker" else serve_all
        asyncio.run(serve(settings, version))
    elif cmd == "worker-health":
        return worker_health()
    else:
        print(f"mixsync {cmd}: not implemented yet", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
