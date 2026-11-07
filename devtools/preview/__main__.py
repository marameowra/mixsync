import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="devtools.preview")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    uvicorn.run(
        "devtools.preview.app:build_app",
        factory=True,
        reload=True,
        reload_dirs=["src", "design", "devtools"],
        reload_includes=["*.py", "*.html", "*.css", "*.js"],
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
