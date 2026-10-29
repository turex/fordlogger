import json
import os
import time
import logging
import secrets
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from pathlib import Path

import requests

log = logging.getLogger("fordlogger")

HTTP_TIMEOUT = 20


def _token_dir(cfg: dict) -> Path:
    """Directory where token files live (FORDLOGGER_TOKEN_DIR, e.g. /app/data in Docker)."""
    return Path(cfg.get("token_dir") or os.getenv("FORDLOGGER_TOKEN_DIR") or ".")


def _write_secret(path: Path, data: dict):
    """Write a token file readable only by the owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # e.g. some bind mounts on Windows/macOS


def _raise_for_status(resp: requests.Response):
    """Like raise_for_status(), but logs Ford's error body first."""
    if not resp.ok:
        log.error("HTTP %s from %s: %s", resp.status_code, resp.url, resp.text[:500])
    resp.raise_for_status()


class _CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    expected_state = None

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        if "code" in params and params.get("state", [None])[0] == _CallbackHandler.expected_state:
            _CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>Authorization successful! You can close this window.</h2>")
        else:
            # Also hit by favicon requests etc.; harmless.
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Missing or invalid code/state")

    def log_message(self, *a):
        pass


def _token_path(cfg: dict) -> Path:
    p = Path(cfg.get("token_file", "tokens.json"))
    # Absolute paths (already resolved by config) are kept as they are.
    return p if p.is_absolute() else _token_dir(cfg) / p


def load_tokens(cfg: dict) -> dict:
    p = _token_path(cfg)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def save_tokens(cfg: dict, tokens: dict):
    _write_secret(_token_path(cfg), tokens)


def _callback_host() -> str:
    # Inside Docker, "localhost" is the container's loopback: published ports
    # (-p 8080:8080) never reach it, so we must listen on all interfaces.
    override = os.getenv("FORDLOGGER_CALLBACK_HOST")
    if override:
        return override
    return "0.0.0.0" if os.path.exists("/.dockerenv") else "localhost"


def do_auth_flow(cfg: dict):
    state = secrets.token_urlsafe(16)
    _CallbackHandler.expected_state = state
    _CallbackHandler.auth_code = None

    params = urlencode({
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": cfg["redirect_uri"],
        "scope": "openid offline_access",
        "state": state,
    })
    login_url = cfg["auth_url"] + "?" + params

    print("\n" + "=" * 60)
    print("Open this URL in your browser:\n")
    print(login_url)
    print("=" * 60 + "\n")

    server = None
    try:
        server = HTTPServer((_callback_host(), 8080), _CallbackHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("Waiting for OAuth callback (max. 120s) ...")
    except OSError as e:
        log.warning("Cannot start callback server on port 8080 (%s); manual paste only.", e)

    if server:
        print("If the automatic callback does not work, press Ctrl+C and paste the")
        print("full URL from your browser's address bar when asked.\n")
        try:
            for _ in range(240):
                if _CallbackHandler.auth_code:
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            print()
        finally:
            server.shutdown()
            server.server_close()

    if _CallbackHandler.auth_code:
        code = _CallbackHandler.auth_code
        log.info("Auth code received via callback: %s...", code[:10])
    else:
        print("\nNo automatic callback received.")
        print("Paste the full callback URL from your browser and press Enter:")
        url = input("> ").strip()
        params = parse_qs(urlparse(url).query)
        if "code" not in params:
            raise ValueError("No 'code' parameter found in the pasted URL")
        code = params["code"][0]
        log.info("Auth code received via manual URL: %s...", code[:10])

    scope = f"{cfg['client_id']} offline_access openid"
    resp = requests.post(cfg["token_url"], data={
        "grant_type": "authorization_code",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri": cfg["redirect_uri"],
        "code": code,
        "scope": scope,
    }, timeout=HTTP_TIMEOUT)
    _raise_for_status(resp)
    tokens = resp.json()
    tokens["obtained_at"] = time.time()

    # Try to detect the VIN and save to tokens_{VIN}.json in the token dir.
    # This allows multiple vehicles without overwriting tokens.json
    try:
        garage = requests.get(
            f"{cfg['api_base']}/garage",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            timeout=15,
        )
        _raise_for_status(garage)
        data = garage.json()
        items = data if isinstance(data, list) else data.get("vehicles", [data])
        vin = next(
            (v.get("vin") or v.get("vehicleId") for v in items if v.get("vin") or v.get("vehicleId")),
            None,
        )
        if vin:
            token_path = _token_dir(cfg) / f"tokens_{vin}.json"
            _write_secret(token_path, tokens)
            log.info("Tokens saved to %s", token_path.resolve())
            return
    except Exception as e:
        log.warning("Could not detect VIN after auth, falling back to tokens.json: %s", e)

    save_tokens(cfg, tokens)
    log.info("Tokens saved to %s", _token_path(cfg).resolve())


def refresh_access_token(cfg: dict, tokens: dict) -> dict:
    log.info("Refreshing access token ...")
    scope = f"{cfg['client_id']} offline_access openid"
    resp = requests.post(cfg["token_url"], data={
        "grant_type": "refresh_token",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": tokens["refresh_token"],
        "redirect_uri": cfg["redirect_uri"],
        "scope": scope,
    }, timeout=HTTP_TIMEOUT)
    _raise_for_status(resp)
    t = resp.json()
    # If the server does not rotate the refresh token, keep the old one.
    t.setdefault("refresh_token", tokens["refresh_token"])
    t["obtained_at"] = time.time()
    save_tokens(cfg, t)
    log.info("Token refreshed, expires in %ss", t.get("expires_in", "?"))
    return t


def get_valid_token(cfg: dict) -> str:
    tokens = load_tokens(cfg)
    if not tokens:
        raise RuntimeError("No tokens found — run: python -m fordlogger --auth")
    needs_refresh = (
        "access_token" not in tokens
        or time.time() > tokens.get("obtained_at", 0) + tokens.get("expires_in", 3600) - 60
    )
    if needs_refresh:
        tokens = refresh_access_token(cfg, tokens)
    return tokens["access_token"]