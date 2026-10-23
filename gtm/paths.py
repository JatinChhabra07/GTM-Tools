"""Where things live. Runtime data and secrets stay off the NTFS mount (plan §1.6).

Every function reads the environment at call time so tests can point
GTM_DATA_DIR / GTM_CONFIG_DIR at a temp directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


class BadSlug(ValueError):
    pass


def check_slug(slug: str) -> str:
    if not _SLUG.match(slug or ""):
        raise BadSlug(
            f"invalid product slug {slug!r}: use 2-40 chars, lowercase letters, digits, '-'"
        )
    return slug


def data_dir() -> Path:
    return Path(os.environ.get("GTM_DATA_DIR", Path.home() / ".local/share/gtm-tools"))


def config_dir() -> Path:
    return Path(os.environ.get("GTM_CONFIG_DIR", Path.home() / ".config/gtm-tools"))


def products_dir() -> Path:
    """Tracked product config (product.yaml, facts.yaml) lives in the repo."""
    return Path(os.environ.get("GTM_PRODUCTS_DIR", REPO_ROOT / "products"))


def product_config_dir(slug: str) -> Path:
    return products_dir() / check_slug(slug)


def product_data_dir(slug: str) -> Path:
    return data_dir() / check_slug(slug)


def state_db(slug: str) -> Path:
    return product_data_dir(slug) / "state.db"


def ledger_db() -> Path:
    return data_dir() / "_global" / "contacts.db"


def runs_dir(slug: str) -> Path:
    return product_data_dir(slug) / "runs"


def secrets_file() -> Path:
    return config_dir() / "env"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path
