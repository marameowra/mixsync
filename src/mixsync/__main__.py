import argparse
import sys

COMMANDS = ("web", "worker", "all", "migrate", "backup", "user-add")


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

        # built here, not by uvicorn: a bad key file must stop startup (build_services loads it)
        uvicorn.run(create_app(), host="0.0.0.0", port=8080)
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

        asyncio.run(serve_worker() if cmd == "worker" else serve_all())
    else:
        print(f"mixsync {cmd}: not implemented yet", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
