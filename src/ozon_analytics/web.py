import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .advertising_queries import advertising
from .auth import verify_password
from .config import settings
from .database import connect
from .queries import dashboard

ROOT = Path(__file__).parent
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
attempts = {}


def configured_users():
    users = {}
    if settings.admin_password:
        users[settings.admin_user] = ("plain", settings.admin_password)
    try:
        extra = json.loads(settings.dashboard_users_json)
    except (ValueError, TypeError):
        extra = {}
    if isinstance(extra, dict):
        for username, password_hash in extra.items():
            if (
                isinstance(username, str)
                and isinstance(password_hash, str)
                and username != settings.admin_user
            ):
                users[username] = ("scrypt", password_hash)
    return users


def credential_tag(username):
    credential = configured_users().get(username)
    return hashlib.sha256(credential[1].encode()).hexdigest() if credential else None


def create_session(now=None, username=None):
    if len(settings.session_secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
    username = username or settings.admin_user
    tag = credential_tag(username)
    if tag is None:
        raise ValueError("Dashboard user is disabled")
    data = base64.urlsafe_b64encode(
        json.dumps(
            [int(now or time.time()) + 7 * 86400, username, tag, secrets.token_hex(16)]
        ).encode()
    ).decode()
    return (
        data
        + "."
        + hmac.new(
            settings.session_secret.encode(), data.encode(), hashlib.sha256
        ).hexdigest()
    )


def valid_session(value, now=None):
    try:
        data, signature = value.split(".")
        expected = hmac.new(
            settings.session_secret.encode(), data.encode(), hashlib.sha256
        ).hexdigest()
        if len(settings.session_secret) < 32 or not hmac.compare_digest(
            signature, expected
        ):
            return False
        decoded = base64.urlsafe_b64decode(data).decode()
        if decoded.startswith("["):
            expiry, username, tag, _nonce = json.loads(decoded)
            current_tag = credential_tag(username)
            return (
                isinstance(expiry, int)
                and expiry > int(now or time.time())
                and current_tag is not None
                and hmac.compare_digest(tag, current_tag)
            )
        # Keep existing primary-account sessions valid until their normal expiry.
        expiry = int(decoded.split(":")[0])
        return bool(settings.admin_password) and expiry > int(now or time.time())
    except (AttributeError, ValueError, TypeError):
        return False


@app.middleware("http")
async def protect(request, call_next):
    public = request.url.path in ("/login", "/healthz") or request.url.path.startswith(
        "/static/"
    )
    if not public and not valid_session(request.cookies.get("ozon_session")):
        response = (
            JSONResponse({"detail": "Требуется вход"}, status_code=401)
            if request.url.path.startswith("/api/")
            else RedirectResponse("/login", status_code=303)
        )
    else:
        response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


@app.get("/healthz")
def health():
    try:
        with connect(web=True) as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable") from None


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return (ROOT / "static/login.html").read_text()


@app.post("/login")
async def login(request: Request):
    # Parsing locally avoids a separate multipart dependency for this tiny form.
    from urllib.parse import parse_qs, urlparse

    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Invalid origin")
    if int(request.headers.get("content-length", "0")) > 4096:
        raise HTTPException(413, "Form too large")
    form = parse_qs((await request.body()).decode())
    ip = request.client.host
    now = time.time()
    recent = [t for t in attempts.get(ip, []) if now - t < 600]
    if len(recent) >= 5:
        raise HTTPException(429, "Слишком много попыток. Повторите через 10 минут.")
    attempts[ip] = recent + [now]
    password = form.get("password", [""])[0]
    username = form.get("username", [""])[0]
    credential = configured_users().get(username)
    authenticated = credential is not None and (
        secrets.compare_digest(password.encode(), credential[1].encode())
        if credential[0] == "plain"
        else verify_password(password, credential[1])
    )
    if not authenticated:
        return RedirectResponse("/login?error=1", status_code=303)
    attempts.pop(ip, None)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "ozon_session",
        create_session(username=username),
        secure=settings.secure_cookies,
        httponly=True,
        samesite="strict",
        max_age=7 * 86400,
    )
    return response


@app.post("/logout")
def logout(request: Request):
    from urllib.parse import urlparse

    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Invalid origin")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        "ozon_session", secure=settings.secure_cookies, httponly=True, samesite="strict"
    )
    return response


@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "static/index.html").read_text()


@app.get("/api/dashboard")
def data(
    date_from: date,
    date_to: date,
    sku: int | None = None,
    cluster: int | None = None,
    warehouse: int | None = None,
):
    if date_to < date_from or (date_to - date_from).days > 366:
        raise HTTPException(422, "Допустимый диапазон: до 366 дней")
    return JSONResponse(
        jsonable_encoder(dashboard(date_from, date_to, sku, cluster, warehouse))
    )


@app.get("/api/advertising")
def advertising_data(
    date_from: date, date_to: date, sku: int | None = None, campaign: int | None = None
):
    if date_to < date_from or (date_to - date_from).days > 366:
        raise HTTPException(422, "Допустимый диапазон: до 366 дней")
    return JSONResponse(
        jsonable_encoder(advertising(date_from, date_to, sku, campaign))
    )
