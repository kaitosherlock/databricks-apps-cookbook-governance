"""Unity Catalog Governance - HTTP entry point.

One process serves two things: the JSON API under ``/api`` and the built
single-page front end everywhere else. That is not a stylistic choice. Databricks
Apps runs exactly one command and installs Python packages only, so a separate
Node server is not available at runtime - the front end is compiled ahead of
time and served as static files from here.

The identity model is unchanged from the Streamlit build: the Databricks Apps
proxy puts the signed-in user in ``X-Forwarded-*`` headers, and every request
re-derives the actor from them. There is no session cookie and no login route,
so there is nothing for a forged request to present.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from api.http import ApiError
from api.routers import activity, assets, diagnostics, grants, principals, session
from ucg.audit import configure_logging
from ucg.errors import Code, GovernanceError, translate

configure_logging()

#: Where ``npm run build`` in ../web puts the compiled front end.
STATIC_DIR = Path(__file__).parent / "static"
INDEX = STATIC_DIR / "index.html"

app = FastAPI(
    title="Quản trị Unity Catalog",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)

for router in (
    session.router, assets.router, grants.router,
    principals.router, activity.router, diagnostics.router,
):
    app.include_router(router, prefix="/api")


# -- error handling -------------------------------------------------------
@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


@app.exception_handler(GovernanceError)
async def _governance_error(request: Request, exc: GovernanceError) -> JSONResponse:
    wrapped = ApiError(exc)
    return JSONResponse(status_code=wrapped.status_code, content=wrapped.detail)


@app.exception_handler(Exception)
async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Nothing reaches a browser without passing through translate().

    SDK exceptions can carry response bodies and response bodies can carry
    credentials, so the raw exception is never serialised - only a stable code,
    a Vietnamese message and a correlation id that appears in the server log.
    """
    err = translate(exc)
    wrapped = ApiError(err)
    return JSONResponse(status_code=wrapped.status_code, content=wrapped.detail)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/api"):
        # Governance data is per-identity and must never sit in a shared cache.
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
def health() -> dict:
    """Liveness only. Says nothing about Databricks, which may be down."""
    return {"ok": True, "ui_built": INDEX.exists()}


# -- static front end -----------------------------------------------------
#: Long-lived assets carry a content hash in the filename, so they are safe to
#: cache hard. index.html never is, or a deploy would not take effect.
if (STATIC_DIR / "_next").is_dir():
    app.mount(
        "/_next",
        StaticFiles(directory=STATIC_DIR / "_next"),
        name="next-assets",
    )


_NOT_BUILT = """Giao diện chưa được build.

Ứng dụng đang chạy và API đã sẵn sàng tại /api, nhưng thư mục static/ trống.

Cách build (cần Node.js 20 trở lên):

    cd web
    npm install
    npm run build

Lệnh build ghi kết quả vào governance-app/static/. Commit thư mục đó rồi deploy
lại, vì Databricks Apps chỉ cài gói Python và không chạy được npm khi deploy.
"""


@app.get("/{full_path:path}")
def spa(full_path: str, request: Request):
    """Serve the built front end, with a client-routing fallback.

    An unknown path is not a 404 here: the front end owns its own routes, so
    anything that is not a real file is handed to index.html and resolved in the
    browser. API paths are excluded - a wrong API path must fail loudly.
    """
    if full_path.startswith("api/"):
        raise ApiError(GovernanceError(
            Code.NOT_FOUND, "Không có endpoint này.", detail=f"/{full_path}"
        ))

    if not INDEX.exists():
        return PlainTextResponse(_NOT_BUILT, status_code=503)

    root = STATIC_DIR.resolve()
    clean = full_path.strip("/")

    if clean:
        candidate = (STATIC_DIR / clean).resolve()
        try:
            # Refuse anything that escapes the static root, whatever the path
            # says. `..` segments and symlinks both land here.
            candidate.relative_to(root)
        except ValueError:
            return FileResponse(INDEX, headers={"Cache-Control": "no-store"})

        if candidate.is_file():
            return FileResponse(candidate)

        # The export is built with trailingSlash, so a route is a directory
        # holding index.html. Without this branch every route would fall through
        # to the root document and the app would redirect to the search page.
        nested = candidate / "index.html"
        if nested.is_file():
            return FileResponse(nested, headers={"Cache-Control": "no-store"})

        flat = root / f"{clean}.html"
        if flat.is_file():
            return FileResponse(flat, headers={"Cache-Control": "no-store"})

    # Unknown path: hand it to the client router rather than 404ing, because the
    # front end owns its own routes.
    return FileResponse(INDEX, headers={"Cache-Control": "no-store"})


def main():
    import uvicorn

    # Databricks Apps injects the port it expects the app to listen on.
    port = int(os.getenv("DATABRICKS_APP_PORT") or os.getenv("PORT") or 8000)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
