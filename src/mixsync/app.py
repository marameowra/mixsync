from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="mixsync")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered by decorator
        return {"status": "ok"}

    return app
