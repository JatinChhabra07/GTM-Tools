"""API keys live in ~/.config/gtm-tools/env (0600, ext4, denied to Claude sessions).

The founder types them into his OWN terminal (`bin/gtm secrets set`), never into chat.
gtm reads them itself; they are never exported into Claude's environment.
"""

from __future__ import annotations

import os

from gtm.paths import ensure_private_dir, secrets_file

KNOWN = {
    "APIFY_API_TOKEN": ("required", "Apify: LinkedIn/X data and people search. console.apify.com -> Settings -> API & Integrations"),
    "SNOV_CLIENT_ID": ("required", "Snov.io API user id. app.snov.io -> Settings -> API"),
    "SNOV_CLIENT_SECRET": ("required", "Snov.io API secret, same page as the client id"),
    "FIRECRAWL_API_KEY": ("optional", "Firecrawl: fallback crawler for sites wigolo can't read"),
    "PRECLEARED_API_KEY": ("optional", "Precleared: per-recipient legal check (off by default)"),
    "X_BEARER_TOKEN": ("optional", "X API, only if you want last30days to read X directly"),
}


def read_all() -> dict[str, str]:
    path = secrets_file()
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def get(name: str) -> str | None:
    return read_all().get(name)


def set_value(name: str, value: str) -> None:
    if name not in KNOWN:
        raise KeyError(f"unknown secret {name}; known: {', '.join(KNOWN)}")
    if not value or "\n" in value:
        raise ValueError("empty or multi-line value")
    data = read_all()
    data[name] = value.strip()
    path = secrets_file()
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# gtm-tools secrets. Do not commit, do not paste into chat.\n")
        for k, v in sorted(data.items()):
            f.write(f"{k}={v}\n")
    os.chmod(path, 0o600)


def status() -> dict[str, str]:
    """Names only ('set'/'missing'), never values: safe to show in a Claude session."""
    data = read_all()
    return {k: ("set" if data.get(k) else "missing") for k in KNOWN}


def file_mode_ok() -> bool:
    path = secrets_file()
    return not path.exists() or (path.stat().st_mode & 0o077) == 0
