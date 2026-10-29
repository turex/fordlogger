import json
import logging
import os
from pathlib import Path

log = logging.getLogger("fordlogger")

DEFAULT_CONFIG = {
    "client_id": "",
    "client_secret": "",
    "redirect_uri": "http://localhost:8080/callback",
    "poll_interval_minutes": 15,
    "auth_url": "https://api.vehicle.ford.com/fcon-public/v1/auth/init",
    "token_url": "https://api.vehicle.ford.com/dah2vb2cprod.onmicrosoft.com/oauth2/v2.0/token?p=B2C_1A_FCON_AUTHORIZE",
    "api_base": "https://api.vehicle.ford.com/fcon-query/v1",
    "db_host": "db",
    "db_port": 5432,
    "db_name": "fordlogger",
    "db_user": "fordlogger",
    "db_password": "fordlogger",
    "sleep_after_minutes": 30,
    "token_file": "tokens.json",
    "token_dir": "",
}

INTERVALS = {
    "driving": 60,
    "charging": 60,
    "parked": 120,
    "sleeping": 300,
}

# Environment variable -> config key (string values)
_ENV_STR = {
    "FORDLOGGER_DB_HOST": "db_host",
    "FORDLOGGER_DB_NAME": "db_name",
    "FORDLOGGER_DB_USER": "db_user",
    "FORDLOGGER_DB_PASSWORD": "db_password",
    "FORDLOGGER_TOKEN_DIR": "token_dir",
}


def _env_bool(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes")


def load_config(path: str = "config.json") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")
    if p.is_dir():
        raise IsADirectoryError(
            f"{p} is a directory, not a file. Docker creates a directory when a bind-mounted "
            f"file does not exist on the host. Stop the stack, remove it and copy "
            f"config.example.json to config.json."
        )
    try:
        user_cfg = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{p} is not valid JSON: {e}") from e

    cfg = {**DEFAULT_CONFIG, **user_cfg}

    # Allow env var overrides for Docker vs local dev
    for env_name, key in _ENV_STR.items():
        if os.environ.get(env_name):
            cfg[key] = os.environ[env_name]
    if os.environ.get("FORDLOGGER_DB_PORT"):
        cfg["db_port"] = int(os.environ["FORDLOGGER_DB_PORT"])

    cfg["store_raw_json"] = _env_bool("FORDLOGGER_STORE_RAW_JSON")
    cfg["geocoding"] = _env_bool("FORDLOGGER_GEOCODING")

    if not cfg["client_id"] or not cfg["client_secret"]:
        raise ValueError("client_id and client_secret must be set in config.json")
    if "your-client" in cfg["client_id"] or "your-client" in cfg["client_secret"]:
        raise ValueError("config.json still contains the placeholder client_id/client_secret")
    return cfg
