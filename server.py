#!/usr/bin/env python3
"""
Spectrum Price Tool — Pipedrive OAuth + Bridge Server

Handles:
  GET /callback       — Pipedrive OAuth code → token exchange (app installation)
  GET /bridge         — Redirect from Pipedrive deal → Zite with dealId
  GET /               — Status page
"""

import base64
import json
import os
import sqlite3
import urllib.parse
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

# ── Config ──────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
ZITE_BASE_URL = "https://dzkrssxbqf.zite.so"

def load_config():
    """Load credentials from config.json file."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}

def get_client_id():
    # Environment variables take priority (for cloud hosting like Render)
    env_val = os.environ.get("PIPEDRIVE_CLIENT_ID", "")
    if env_val:
        return env_val
    return load_config().get("client_id", "")

def get_client_secret():
    env_val = os.environ.get("PIPEDRIVE_CLIENT_SECRET", "")
    if env_val:
        return env_val
    return load_config().get("client_secret", "")

def get_redirect_uri():
    env_val = os.environ.get("REDIRECT_URI", "")
    if env_val:
        return env_val
    return load_config().get("redirect_uri", "")

# ── Database ────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "tokens.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT,
            company_domain TEXT,
            user_id TEXT,
            access_token TEXT,
            refresh_token TEXT,
            expires_in INTEGER,
            token_type TEXT,
            scope TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS install_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def log_event(event: str, detail: str = ""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO install_log (event, detail) VALUES (?, ?)", (event, detail))
    conn.commit()
    conn.close()

def store_tokens(token_data: dict, user_info: dict = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO tokens (company_id, company_domain, user_id, access_token, refresh_token, expires_in, token_type, scope)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(user_info.get("company_id", "")) if user_info else "",
        user_info.get("company_domain", "") if user_info else "",
        str(user_info.get("id", "")) if user_info else "",
        token_data.get("access_token", ""),
        token_data.get("refresh_token", ""),
        token_data.get("expires_in", 0),
        token_data.get("token_type", ""),
        token_data.get("scope", ""),
    ))
    conn.commit()
    conn.close()

def get_install_count():
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
    conn.close()
    return count

# ── App ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── OAuth Callback ──────────────────────────────────────────────────
@app.get("/callback")
async def oauth_callback(request: Request, code: str = None, error: str = None, state: str = None):
    """Handle Pipedrive OAuth callback after user clicks 'Allow and Install'."""

    log_event("callback_received", f"code={'yes' if code else 'no'}, error={error}")

    # Handle user denial
    if error:
        log_event("install_denied", error)
        return HTMLResponse(content=error_page("Installation Cancelled", "You chose not to install the app. You can try again from the Pipedrive Marketplace."), status_code=200)

    # Handle missing code
    if not code:
        log_event("no_code", str(dict(request.query_params)))
        return HTMLResponse(content=error_page("Missing Authorization Code", "No authorization code was received from Pipedrive. Please try installing again."), status_code=400)

    # Check credentials
    CLIENT_ID = get_client_id()
    CLIENT_SECRET = get_client_secret()
    if not CLIENT_ID or not CLIENT_SECRET:
        log_event("missing_credentials", "client_id or client_secret not set in config.json")
        return HTMLResponse(content=error_page(
            "Server Configuration Error",
            "The app credentials are not configured. Please contact the administrator."
        ), status_code=500)

    # ── Exchange code for tokens ────────────────────────────────────
    try:
        auth_string = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

        # The redirect_uri must exactly match what's registered in the Pipedrive app.
        redirect_uri = get_redirect_uri()
        if not redirect_uri:
            # Fallback: reconstruct from the request (may not work behind proxy)
            redirect_uri = str(request.url).split("?")[0]

        log_event("token_exchange_start", f"redirect_uri={redirect_uri}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://oauth.pipedrive.com/oauth/token",
                headers={
                    "Authorization": f"Basic {auth_string}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )

        log_event("token_exchange_response", f"status={resp.status_code}")

        if resp.status_code != 200:
            log_event("token_exchange_failed", resp.text[:500])
            return HTMLResponse(content=error_page(
                "Token Exchange Failed",
                f"Pipedrive returned status {resp.status_code}. This may happen if the authorization code expired (5-minute window). Please try installing again.<br><br><small>Detail: {resp.text[:200]}</small>"
            ), status_code=400)

        token_data = resp.json()
        log_event("token_exchange_success", f"token_type={token_data.get('token_type')}")

        # ── Fetch user info ─────────────────────────────────────────
        user_info = {}
        access_token = token_data.get("access_token", "")
        api_domain = token_data.get("api_domain", "https://api.pipedrive.com")

        if access_token:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    me_resp = await client.get(
                        f"{api_domain}/v1/users/me",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                if me_resp.status_code == 200:
                    me_data = me_resp.json()
                    user_info = me_data.get("data", {})
                    log_event("user_info_fetched", f"company_domain={user_info.get('company_domain')}")
            except Exception as e:
                log_event("user_info_error", str(e))

        # Store the tokens
        store_tokens(token_data, user_info)

        company = user_info.get("company_domain", "your Pipedrive account")
        user_name = user_info.get("name", "")

        return HTMLResponse(content=success_page(company, user_name))

    except Exception as e:
        log_event("callback_error", str(e))
        return HTMLResponse(content=error_page(
            "Unexpected Error",
            f"Something went wrong during installation: {str(e)}"
        ), status_code=500)


# ── Bridge Redirect ─────────────────────────────────────────────────
@app.get("/bridge")
async def bridge_redirect(request: Request):
    """
    Redirect from Pipedrive → Zite with the deal ID.
    Pipedrive Link Action sends: ?resource=deal&view=details&selectedIds=<DEAL_ID>&userId=...&companyId=...
    We extract selectedIds and redirect to Zite with ?dealId=<DEAL_ID>
    """
    params = dict(request.query_params)
    deal_id = params.get("selectedIds") or params.get("dealId") or params.get("deal_id")

    if deal_id:
        zite_url = f"{ZITE_BASE_URL}?dealId={urllib.parse.quote(str(deal_id))}"
        return HTMLResponse(content=bridge_page(deal_id, zite_url))
    else:
        return HTMLResponse(content=bridge_error_page())


# ── Status Page ─────────────────────────────────────────────────────
@app.get("/")
async def status_page():
    install_count = get_install_count()
    configured = bool(get_client_id() and get_client_secret())
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Spectrum Price Tool — Server</title>
  <style>{base_css()}</style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
    </div>
    <h1>Spectrum Price Tool</h1>
    <p class="subtitle">Pipedrive → Zite Bridge Server</p>
    <div class="status-grid">
      <div class="status-item">
        <span class="status-label">OAuth Credentials</span>
        <span class="status-value {'status-ok' if configured else 'status-warn'}">{'Configured' if configured else 'Not Set'}</span>
      </div>
      <div class="status-item">
        <span class="status-label">Installations</span>
        <span class="status-value status-ok">{install_count}</span>
      </div>
    </div>
    <div class="endpoints">
      <p class="endpoint"><code>GET /callback</code> — OAuth callback for Pipedrive</p>
      <p class="endpoint"><code>GET /bridge?selectedIds=123</code> — Deal → Zite redirect</p>
    </div>
  </div>
</body>
</html>""")


# ── HTML Templates ──────────────────────────────────────────────────

def base_css():
    return """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #0f1729;
      color: #e2e8f0;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }
    .card {
      background: #1a2332;
      border: 1px solid #2d3a4a;
      border-radius: 12px;
      padding: 40px 48px;
      max-width: 520px;
      width: 90%;
      text-align: center;
    }
    .logo {
      width: 48px;
      height: 48px;
      margin: 0 auto 20px;
      background: #22d3ee;
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .logo svg {
      width: 28px;
      height: 28px;
      fill: #0f1729;
    }
    .logo-success {
      background: #10b981;
    }
    .logo-error {
      background: #ef4444;
    }
    h1 {
      font-size: 20px;
      font-weight: 600;
      color: #f1f5f9;
      margin-bottom: 8px;
    }
    .subtitle {
      font-size: 14px;
      color: #94a3b8;
      margin-bottom: 24px;
      line-height: 1.5;
    }
    .status-grid {
      display: flex;
      gap: 16px;
      justify-content: center;
      margin-bottom: 24px;
    }
    .status-item {
      background: #162032;
      border: 1px solid #2d3a4a;
      border-radius: 8px;
      padding: 12px 20px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .status-label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #64748b;
    }
    .status-value {
      font-size: 14px;
      font-weight: 600;
    }
    .status-ok { color: #10b981; }
    .status-warn { color: #f59e0b; }
    .endpoints {
      text-align: left;
      background: #162032;
      border: 1px solid #2d3a4a;
      border-radius: 8px;
      padding: 16px 20px;
    }
    .endpoint {
      font-size: 13px;
      color: #94a3b8;
      margin-bottom: 8px;
      line-height: 1.4;
    }
    .endpoint:last-child { margin-bottom: 0; }
    .endpoint code {
      color: #22d3ee;
      font-family: 'SF Mono', 'Fira Code', monospace;
      font-size: 12px;
    }
    .deal-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: #162032;
      border: 1px solid #2d3a4a;
      border-radius: 8px;
      padding: 10px 20px;
      margin-bottom: 24px;
      font-size: 14px;
    }
    .deal-badge .label { color: #64748b; }
    .deal-badge .value {
      color: #22d3ee;
      font-weight: 600;
      font-family: 'SF Mono', 'Fira Code', monospace;
    }
    .spinner {
      width: 24px;
      height: 24px;
      border: 3px solid #2d3a4a;
      border-top-color: #22d3ee;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin: 0 auto 16px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .info-box {
      background: #162032;
      border: 1px solid #2d3a4a;
      border-radius: 8px;
      padding: 16px 20px;
      margin-top: 20px;
      text-align: left;
    }
    .info-box p {
      font-size: 13px;
      color: #94a3b8;
      line-height: 1.6;
      margin-bottom: 8px;
    }
    .info-box p:last-child { margin-bottom: 0; }
    .info-box strong { color: #e2e8f0; }
    .error-box {
      background: #2d1b1b;
      border: 1px solid #5c2828;
      border-radius: 8px;
      padding: 16px 20px;
      margin-top: 20px;
    }
    .error-box p {
      color: #fca5a5;
      font-size: 13px;
      line-height: 1.5;
    }
    .btn {
      display: inline-block;
      margin-top: 20px;
      padding: 10px 24px;
      background: #22d3ee;
      color: #0f1729;
      text-decoration: none;
      font-weight: 600;
      font-size: 14px;
      border-radius: 8px;
      transition: background 0.15s;
    }
    .btn:hover { background: #06b6d4; }
    .btn-secondary {
      background: #2d3a4a;
      color: #e2e8f0;
    }
    .btn-secondary:hover { background: #3d4a5a; }
    .check-icon {
      width: 48px;
      height: 48px;
      margin: 0 auto 20px;
      background: #10b981;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .check-icon svg {
      width: 28px;
      height: 28px;
      fill: white;
    }
    """


def success_page(company: str, user_name: str):
    greeting = f"Hi {user_name}! " if user_name else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Spectrum Price Tool — Installed</title>
  <style>{base_css()}</style>
</head>
<body>
  <div class="card">
    <div class="check-icon">
      <svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
    </div>
    <h1>Installation Complete</h1>
    <p class="subtitle">{greeting}Spectrum Price Tool has been installed on <strong>{company}</strong>.</p>
    <div class="info-box">
      <p><strong>What happens next:</strong></p>
      <p>1. Go to any <strong>Deal</strong> in Pipedrive</p>
      <p>2. Click the <strong>"⋯" (more actions)</strong> menu</p>
      <p>3. Click <strong>"Generate Price"</strong></p>
      <p>4. You'll be taken to the Zite pricing calculator with the Deal ID pre-filled</p>
    </div>
    <a class="btn" href="https://{company}.pipedrive.com" target="_blank">Go to Pipedrive</a>
  </div>
</body>
</html>"""


def error_page(title: str, message: str):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Spectrum Price Tool — Error</title>
  <style>{base_css()}</style>
</head>
<body>
  <div class="card">
    <div class="logo logo-error">
      <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>
    </div>
    <h1>{title}</h1>
    <div class="error-box">
      <p>{message}</p>
    </div>
  </div>
</body>
</html>"""


def bridge_page(deal_id: str, zite_url: str):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Spectrum Price Tool — Redirecting</title>
  <style>{base_css()}</style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
    </div>
    <h1>Spectrum Price Tool</h1>
    <p class="subtitle">Opening the Fluoron Sleeve Pricing Calculator...</p>
    <div class="deal-badge">
      <span class="label">Pipedrive Deal</span>
      <span class="value">#{deal_id}</span>
    </div>
    <div class="spinner"></div>
    <p style="font-size: 13px; color: #64748b;">Redirecting to Zite...</p>
  </div>
  <script>
    setTimeout(function() {{
      window.location.href = "{zite_url}";
    }}, 800);
  </script>
</body>
</html>"""


def bridge_error_page():
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Spectrum Price Tool — No Deal ID</title>
  <style>{base_css()}</style>
</head>
<body>
  <div class="card">
    <div class="logo logo-error">
      <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>
    </div>
    <h1>No Deal ID Found</h1>
    <p class="subtitle">This page should be opened from a Pipedrive deal. The deal ID was not included in the URL.</p>
    <a class="btn" href="{ZITE_BASE_URL}" target="_self">Open Price Tool Manually</a>
  </div>
</body>
</html>"""


# ── Setup endpoint ──────────────────────────────────────────────────
@app.post("/setup")
async def setup_credentials(request: Request):
    """Set up OAuth credentials. POST JSON: {client_id, client_secret, redirect_uri}"""
    try:
        data = await request.json()
        config = load_config()
        if "client_id" in data:
            config["client_id"] = data["client_id"]
        if "client_secret" in data:
            config["client_secret"] = data["client_secret"]
        if "redirect_uri" in data:
            config["redirect_uri"] = data["redirect_uri"]
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        log_event("config_updated", f"keys={list(data.keys())}")
        return {"status": "ok", "configured_keys": list(config.keys())}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ── Debug endpoint ──────────────────────────────────────────────────
@app.get("/debug/log")
async def debug_log():
    """Show recent install log entries for troubleshooting."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT event, detail, created_at FROM install_log ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    entries = [{"event": r[0], "detail": r[1], "time": r[2]} for r in rows]
    return {"log": entries}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
