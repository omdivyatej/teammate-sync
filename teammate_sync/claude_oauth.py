"""
Automatic Claude authorization via in-app OAuth (localhost redirect).

`claude setup-token` is an out-of-band paste flow (Anthropic shows a code you
must copy back), so it can't be automated. Instead we drive the same OAuth
ourselves — the way Claude Code's interactive /login does — pointing the
redirect at OUR localhost server so we catch the code automatically: browser
opens -> user authorizes -> redirect to localhost -> we exchange the code for
the long-lived token. No paste, no terminal.

Params are Claude Code's public OAuth client (observed from `claude
setup-token`'s own authorize URL). Endpoints can shift between Claude versions,
so token exchange tries several known hosts and everything is logged.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import threading
import urllib.parse
from pathlib import Path

import httpx

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
# Token exchange host has shifted across versions (console.anthropic.com ->
# platform.claude.com). Try the known ones in order.
TOKEN_ENDPOINTS = [
    "https://console.anthropic.com/v1/oauth/token",
    "https://platform.claude.com/v1/oauth/token",
    "https://claude.com/cai/oauth/token",
]
SCOPE = "user:inference"

_TOKEN_RE = __import__("re").compile(r"sk-ant-[a-z]+\d{2}-[A-Za-z0-9_-]{20,}")


def _log(msg: str) -> None:
    p = Path("~/.teammate-sync/state/setup-token.log").expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(msg.rstrip() + "\n")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def authorize() -> tuple[bool, str]:
    """Run the full in-app OAuth. Returns (ok, message). Stores the token on
    success; never logs the token itself."""
    from .auth import write_claude_token

    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)

    # Local callback server on a free port.
    captured: dict[str, str] = {}
    ev = threading.Event()

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured["code"] = (q.get("code") or [""])[0]
            captured["state"] = (q.get("state") or [""])[0]
            body = (b"<!doctype html><html><body style='font-family:-apple-system,"
                    b"sans-serif;background:#0a0b0e;color:#e8e8ea;text-align:center;"
                    b"padding:4em 1em'><h2 style='color:#00E57A'>Authorized &#10003;</h2>"
                    b"<p>You can close this tab and return to CodeBaton.</p></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            ev.set()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    redirect_uri = f"http://localhost:{port}/callback"

    server = http.server.HTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    authz = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    _log(f"--- oauth start; redirect={redirect_uri} ---")
    _log(f"opening: {authz[:90]}...")
    import webbrowser
    webbrowser.open(authz)

    got = ev.wait(timeout=300)
    server.shutdown()
    if not got or not captured.get("code"):
        _log("--- result: no code received (timed out / cancelled) ---")
        return False, "Authorization wasn't completed in the browser."
    if captured.get("state") != state:
        _log("--- result: state mismatch ---")
        return False, "Authorization failed a security check (state mismatch)."

    code = captured["code"].split("#")[0].split("&")[0]

    # Exchange the code for the token. redirect_uri must match the authorize one.
    last_err = ""
    for ep in TOKEN_ENDPOINTS:
        try:
            r = httpx.post(
                ep,
                json={
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                    "state": state,
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=30,
            )
        except httpx.HTTPError as e:
            last_err = f"{ep}: {e}"
            _log(f"token endpoint error {ep}: {e}")
            continue
        if r.status_code == 200:
            data = r.json()
            tok = data.get("access_token") or ""
            if _TOKEN_RE.fullmatch(tok) or tok.startswith("sk-ant-"):
                write_claude_token(tok)
                _log(f"--- result: token stored via {ep} (len {len(tok)}) ---")
                return True, "Claude authorized for background capture + live answers."
            last_err = f"{ep}: 200 but no usable access_token (keys={list(data)})"
            _log(last_err)
        else:
            last_err = f"{ep}: HTTP {r.status_code}: {r.text[:200]}"
            _log(last_err)

    return False, f"Token exchange failed. See setup-token.log. ({last_err[:120]})"
