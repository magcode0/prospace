import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.db import SessionLocal
from app.routers import auth, balance, chat, client, freelancer, notifications, profile, staff, support
from app.web import LoginRequired, OptionalUser, Session, home_url, redirect, render, templates

logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name, docs_url="/api/docs", redoc_url=None)

Path(settings.media_dir, "avatars").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")


@app.middleware("http")
async def same_origin_guard(request: Request, call_next):
    """CSRF-защита форм: POST принимаем только со своего origin (cookie и так SameSite=Lax)."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-origin request blocked", status_code=403)
    return await call_next(request)


app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", https_only=False)

for r in (auth, client, freelancer, profile, notifications, chat, balance, support, staff):
    app.include_router(r.router)


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    return redirect("/login")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    async with SessionLocal() as session:
        return await render(request, session, "error.html", None, status_code=exc.status_code, code=exc.status_code, detail=exc.detail)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session: Session, user: OptionalUser):
    if user:
        return redirect(home_url(user))
    return await render(request, session, "index.html")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


__all__ = ["app", "templates"]
