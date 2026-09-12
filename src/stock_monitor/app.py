from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .settings import CredentialsError, UnsafeCredentialsError, credentials_path, load_credentials


def create_app() -> FastAPI:
    app = FastAPI(title="Stock Monitor", docs_url=None, redoc_url=None)

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/configuration")
    def configuration() -> JSONResponse:
        try:
            load_credentials(credentials_path())
        except UnsafeCredentialsError as error:
            return JSONResponse(
                status_code=503,
                content={"status": "invalid", "reason": error.code},
            )
        except CredentialsError:
            return JSONResponse(
                status_code=503,
                content={"status": "missing", "reason": "toss_not_configured"},
            )
        return JSONResponse(content={"status": "configured", "toss": "not_checked"})

    return app


app = create_app()
