"""FastAPI app factory: uvicorn app.main:app"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import scheduler
from app.auth import LoginRequired
from app.config import Settings, get_settings
from app.db import Database, upgrade
from app.factories import Providers
from app.services.generation import GenerationService, UserError

ROOT = Path(__file__).resolve().parent.parent


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Request logging would print OAuth codes/tokens in URLs and bodies.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("apscheduler", "alembic"):  # per-tick / per-migration chatter
        logging.getLogger(name).setLevel(logging.WARNING)


def _asset_url(path: str) -> str:
    """/static URL with the file's mtime appended, so browsers never keep a stale copy after an update."""
    try:
        version = int((ROOT / "static" / path).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


def create_app(settings: Settings | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()
    fatal = settings.fatal_problems()
    if fatal:
        raise RuntimeError("Configuration problems:\n  - " + "\n  - ".join(fatal))
    _configure_logging()
    settings.VAR_DIR.mkdir(parents=True, exist_ok=True)
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    upgrade(settings.DATABASE_URL)

    db = Database(settings.DATABASE_URL)
    providers = Providers(settings, db, transport)
    service = GenerationService(providers)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched = scheduler.start(service) if settings.SCHEDULER_ENABLED else None
        yield
        if sched:
            sched.shutdown(wait=False)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.providers = providers
    app.state.service = service
    app.state.templates = Jinja2Templates(directory=str(ROOT / "templates"))
    app.state.templates.env.globals["asset"] = _asset_url
    app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, session_cookie="vs_session",
                       max_age=14 * 24 * 3600, same_site="lax", https_only=settings.COOKIE_SECURE)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(UserError)
    async def _user_error(request: Request, exc: UserError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    from app.routes import api, pages, settings_routes

    app.include_router(pages.router)
    app.include_router(api.router)
    app.include_router(settings_routes.router)
    return app


def __getattr__(name: str):
    # `uvicorn app.main:app` builds the app lazily so importing this module stays side-effect free.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
