"""`gtm schedule install` (plan §5 Phase 8): user crontab entries (no sudo, no linger needed).

Times in product.yaml are in the product's timezone; cron runs in the machine's local time, so we
convert. Our lines carry a `# gtm-tools:<slug>` marker and are replaced as a block.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from gtm.config import ProductConfig
from gtm.paths import REPO_ROOT

WRAPPER = REPO_ROOT / "bin" / "gtm-scheduled-run"


def _local_hm(hhmm: str, tz: str) -> tuple[int, int]:
    h, m = (int(x) for x in hhmm.split(":"))
    src = datetime.now(ZoneInfo(tz)).replace(hour=h, minute=m, second=0, microsecond=0)
    local = src.astimezone()  # the machine's timezone
    return local.hour, local.minute


def lines_for(cfg: ProductConfig, *, feedback: bool) -> list[str]:
    slug = cfg.product.slug
    tag = f"# gtm-tools:{slug}"
    out = []
    for slot in cfg.schedule.runs:
        h, m = _local_hm(slot.at, cfg.schedule.timezone)
        out.append(f"{m} {h} * * * {WRAPPER} {slug} {slot.mode} {tag}")
    # hourly reply/suppression sync + outbox drain, offset from the full runs
    out.append(f"17 * * * * {WRAPPER} {slug} sync {tag}")
    if feedback:
        h, m = _local_hm("08:30", cfg.schedule.timezone)
        out.append(f"{m} {h} * * * {WRAPPER} {slug} feedback {tag}")
    return out


def current() -> str:
    if not shutil.which("crontab"):
        raise RuntimeError("crontab not found")
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def _write(text: str) -> None:
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def install(cfg: ProductConfig, *, feedback: bool) -> list[str]:
    tag = f"# gtm-tools:{cfg.product.slug}"
    kept = [line for line in current().splitlines() if not line.rstrip().endswith(tag)]
    new = lines_for(cfg, feedback=feedback)
    _write("\n".join(kept + new).strip() + "\n")
    return new


def remove(slug: str) -> int:
    tag = f"# gtm-tools:{slug}"
    lines = current().splitlines()
    kept = [line for line in lines if not line.rstrip().endswith(tag)]
    _write("\n".join(kept).strip() + "\n" if kept else "")
    return len(lines) - len(kept)


def show(slug: str) -> list[str]:
    return [line for line in current().splitlines() if line.rstrip().endswith(f"# gtm-tools:{slug}")]
