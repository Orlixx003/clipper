"""User settings + license activation. Settings live in the user's profile, so they survive updates:
Windows: %APPDATA%\\Clipper\\config.json   Mac/Linux: ~/.clipper/config.json
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import product

RECHECK_DAYS = 7      # re-verify the license online once a week
OFFLINE_GRACE_DAYS = 30  # keep working offline this long after the last successful check


def config_dir():
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA") or Path.home())
        d = base / "Clipper"
    else:
        d = Path.home() / ".clipper"
    d.mkdir(parents=True, exist_ok=True)
    return d


CONFIG_FILE = config_dir() / "config.json"

DEFAULTS = {
    "license_key": "",
    "license_ok_at": 0,         # unix time of the last successful online check
    "ai": "auto",               # "auto" | "ollama" (free, local) | "claude" (user's own API key)
    "claude_key": "",
    "claude_model": "",         # empty = engine default (CLAUDE_MODEL in .env or claude-sonnet-5-5)
    "ollama_model": "",         # empty = engine default (qwen2.5:7b)
}


def load():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ───────────────────────── license ─────────────────────────

def licensing_enabled():
    return bool(product.GUMROAD_PRODUCT_ID.strip())


def verify_online(key):
    """Returns (ok: bool, message: str). Raises OSError if there is no internet."""
    data = urllib.parse.urlencode({
        "product_id": product.GUMROAD_PRODUCT_ID.strip(),
        "license_key": key.strip(),
        "increment_uses_count": "false",
    }).encode()
    req = urllib.request.Request("https://api.gumroad.com/v2/licenses/verify", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:   # Gumroad answers 404 for unknown keys
        try:
            res = json.loads(ex.read().decode())
        except Exception:
            return False, f"License server error ({ex.code})."
    if not res.get("success"):
        return False, res.get("message") or "This license key is not valid."
    p = res.get("purchase", {})
    if p.get("refunded") or p.get("chargebacked"):
        return False, "This purchase was refunded, so the license is no longer active."
    if p.get("subscription_ended_at") or p.get("subscription_cancelled_at") or p.get("subscription_failed_at"):
        return False, "The subscription for this license is not active."
    return True, f"Activated for {p.get('email', 'your account')}. Thank you!"


def activate(key):
    cfg = load()
    try:
        ok, msg = verify_online(key)
    except OSError:
        return False, "No internet connection. Connect to the internet to activate."
    if ok:
        cfg["license_key"] = key.strip()
        cfg["license_ok_at"] = time.time()
        save(cfg)
    return ok, msg


def is_licensed():
    """(ok, message). Checks online once a week; works offline within the grace period."""
    if not licensing_enabled():
        return True, "Personal edition (licensing off)."
    cfg = load()
    if not cfg["license_key"]:
        return False, "Not activated."
    age_days = (time.time() - cfg["license_ok_at"]) / 86400
    if age_days < RECHECK_DAYS:
        return True, "Activated."
    try:
        ok, msg = verify_online(cfg["license_key"])
    except OSError:
        if age_days < OFFLINE_GRACE_DAYS:
            return True, "Activated (offline)."
        return False, "Please connect to the internet once to re-check your license."
    if ok:
        cfg["license_ok_at"] = time.time()
        save(cfg)
        return True, "Activated."
    return False, msg


def apply_to_env(cfg=None):
    """Make the core engine use the user's AI settings."""
    cfg = cfg or load()
    if cfg.get("claude_key"):           # a key from Settings wins over .env
        os.environ["ANTHROPIC_API_KEY"] = cfg["claude_key"].strip()
