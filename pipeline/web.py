"""FastAPI web app for multi-user onboarding."""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

load_dotenv()

from pipeline.users import BASE_DIR, create_user, get_user, update_status, user_dir

logger = logging.getLogger(__name__)

app = FastAPI(title="LinkedIn Pipeline")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_vnc_sessions: dict[str, dict] = {}
_NEXT_DISPLAY = 10
_NEXT_WS_PORT = 6080


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request, name: str = Form(...),
                       telegram_chat_id: str = Form(...)):
    user_id = re.sub(r"[^a-z0-9]", "", name.lower())
    if not user_id:
        return templates.TemplateResponse(request, "setup.html", {
            "error": "Name must contain letters or numbers"
        })
    if get_user(user_id):
        return templates.TemplateResponse(request, "setup.html", {
            "error": f"User '{user_id}' already exists"
        })
    if not re.match(r"-?\d+$", telegram_chat_id):
        return templates.TemplateResponse(request, "setup.html", {
            "error": "Chat ID must be a number"
        })

    create_user(user_id, name, telegram_chat_id)
    return RedirectResponse(f"/login/{user_id}", status_code=303)


def _start_vnc(user_id: str) -> int:
    global _NEXT_DISPLAY, _NEXT_WS_PORT

    if user_id in _vnc_sessions:
        return _vnc_sessions[user_id]["ws_port"]

    if len(_vnc_sessions) >= 1:
        raise RuntimeError("Only one VNC session at a time (RAM constraint)")

    display = _NEXT_DISPLAY
    ws_port = _NEXT_WS_PORT
    vnc_port = 5900 + display
    _NEXT_DISPLAY += 1
    _NEXT_WS_PORT += 1

    profile = str(user_dir(user_id) / "profile")
    pids = []

    xvfb = subprocess.Popen(
        ["Xvfb", f":{display}", "-screen", "0", "1280x900x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pids.append(xvfb.pid)

    env = {**os.environ, "DISPLAY": f":{display}"}
    openbox = subprocess.Popen(
        ["openbox"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pids.append(openbox.pid)

    chromium = subprocess.Popen([
        "chromium", "--no-sandbox",
        f"--user-data-dir={profile}",
        f"--display=:{display}",
        "--disable-gpu", "--disable-software-rasterizer",
        "--disable-gpu-compositing",
        "--window-size=1280,900",
        "https://www.linkedin.com/login",
    ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids.append(chromium.pid)

    x11vnc = subprocess.Popen([
        "x11vnc", "-display", f":{display}",
        "-nopw", "-forever", "-shared",
        "-rfbport", str(vnc_port),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids.append(x11vnc.pid)

    websockify_proc = subprocess.Popen([
        "websockify", "--web", "/usr/share/novnc",
        str(ws_port), f"localhost:{vnc_port}",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids.append(websockify_proc.pid)

    _vnc_sessions[user_id] = {"pids": pids, "ws_port": ws_port}
    logger.info("Started VNC session for %s (display=:%d, ws_port=%d)", user_id, display, ws_port)
    return ws_port


def _stop_vnc(user_id: str) -> None:
    session = _vnc_sessions.pop(user_id, None)
    if not session:
        return
    for pid in reversed(session["pids"]):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    logger.info("Stopped VNC session for %s", user_id)


@app.get("/login/{user_id}", response_class=HTMLResponse)
async def login_page(request: Request, user_id: str):
    user = get_user(user_id)
    if not user:
        return HTMLResponse("User not found", status_code=404)

    try:
        ws_port = _start_vnc(user_id)
        host = request.headers.get("host", "localhost").split(":")[0]
        vnc_url = f"http://{host}:{ws_port}/vnc.html?autoconnect=true&resize=scale"
    except RuntimeError as e:
        return templates.TemplateResponse(request, "login.html", {
            "user_id": user_id, "vnc_url": None,
            "error": str(e),
        })

    return templates.TemplateResponse(request, "login.html", {
        "user_id": user_id, "vnc_url": vnc_url,
        "error": None,
    })


@app.post("/login/{user_id}/done")
async def login_done(user_id: str):
    _stop_vnc(user_id)

    from pipeline.scraper import heartbeat

    profile = user_dir(user_id) / "profile"
    alive = await heartbeat(profile_dir=profile)

    if alive:
        update_status(user_id, "pending_calendar")
        return RedirectResponse(f"/calendar/{user_id}", status_code=303)

    return RedirectResponse(f"/login/{user_id}?error=session_invalid", status_code=303)


@app.get("/calendar/{user_id}", response_class=HTMLResponse)
async def calendar_page(request: Request, user_id: str):
    user = get_user(user_id)
    if not user:
        return HTMLResponse("User not found", status_code=404)

    from google_auth_oauthlib.flow import Flow

    creds_file = BASE_DIR / "credentials.json"
    if not creds_file.exists():
        return HTMLResponse("Google credentials.json not found on server", status_code=500)

    host = request.headers.get("host", "localhost:8080")
    redirect_uri = f"http://{host}/calendar/{user_id}/callback"

    flow = Flow.from_client_secrets_file(
        str(creds_file),
        scopes=["https://www.googleapis.com/auth/calendar.events"],
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    return templates.TemplateResponse(request, "calendar.html", {
        "auth_url": auth_url, "error": None,
    })


@app.get("/calendar/{user_id}/callback")
async def calendar_callback(user_id: str, code: str, request: Request):
    from google_auth_oauthlib.flow import Flow

    creds_file = BASE_DIR / "credentials.json"
    host = request.headers.get("host", "localhost:8080")
    redirect_uri = f"http://{host}/calendar/{user_id}/callback"

    flow = Flow.from_client_secrets_file(
        str(creds_file),
        scopes=["https://www.googleapis.com/auth/calendar.events"],
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)

    token_path = user_dir(user_id) / "token.json"
    token_path.write_text(flow.credentials.to_json(), encoding="utf-8")

    update_status(user_id, "active")

    user = get_user(user_id)
    return templates.TemplateResponse(request, "success.html", {
        "name": user["name"],
    })
