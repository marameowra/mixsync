import argparse
import sys

COMMANDS = ("web", "worker", "all", "migrate", "backup")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mixsync")
    parser.add_argument("command", choices=COMMANDS)
    cmd: str = parser.parse_args(argv).command
    if cmd == "web":
        import uvicorn

        uvicorn.run("mixsync.app:create_app", factory=True, host="0.0.0.0", port=8080)
    elif cmd == "migrate":
        from mixsync.db.migrate import upgrade

        upgrade()
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
