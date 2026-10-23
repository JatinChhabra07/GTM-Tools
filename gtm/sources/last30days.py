"""last30days engine (github.com/mvanhorn/last30days-skill) for Reddit/HN pain language (plan §1.4).

Installed at a pinned tag under the data dir and run as a subprocess in JSON mode. Its output is
anonymous community text: it becomes `pain_language` for the writer's vocabulary, never a lead.

Isolation: the subprocess gets a private HOME (so it can't read your browser cookies or configs),
`--no-browser-cookies`, and no API keys from our environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from gtm.paths import data_dir, ensure_private_dir

REPO = "https://github.com/mvanhorn/last30days-skill"
TAG = "v3.30.0"
SCRIPT = Path("skills/last30days/scripts/last30days.py")


class Last30Error(Exception):
    pass


def install_dir() -> Path:
    return Path(os.environ.get("GTM_LAST30DAYS_DIR", data_dir() / "vendor" / "last30days-skill"))


def installed() -> bool:
    return (install_dir() / SCRIPT).exists()


def install() -> dict:
    """Clone (or move) the checkout to the pinned tag. Idempotent."""
    target = install_dir()
    ensure_private_dir(target.parent)
    if not (target / ".git").exists():
        proc = subprocess.run(["git", "clone", "--quiet", REPO, str(target)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise Last30Error(f"git clone failed: {proc.stderr.strip()[:300]}")
    subprocess.run(["git", "-C", str(target), "fetch", "--quiet", "--tags"], capture_output=True)
    proc = subprocess.run(["git", "-C", str(target), "checkout", "--quiet", TAG], capture_output=True, text=True)
    if proc.returncode != 0:
        raise Last30Error(f"checkout {TAG} failed: {proc.stderr.strip()[:300]}")
    sha = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    return {"path": str(target), "tag": TAG, "commit": sha}


def _env() -> dict[str, str]:
    home = ensure_private_dir(data_dir() / "vendor" / "last30days-home")
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8"}


def run(topic: str, *, sources: tuple[str, ...] = ("reddit", "hackernews"),
        subreddits: list[str] | None = None, timeout: int = 300, mock: bool = False) -> dict:
    """Raw JSON export for one topic. Python >= 3.12 (ours)."""
    if os.environ.get("GTM_FAKE") == "1":
        from gtm import fake
        return fake.last30days(topic)
    if not installed():
        raise Last30Error("last30days is not installed: run `bin/gtm tools install`")
    cmd = [sys.executable, str(install_dir() / SCRIPT), topic, "--emit=json", "--json-profile", "raw",
           "--search", ",".join(sources), "--no-browser-cookies", "--quick"]
    if subreddits:
        cmd += ["--subreddits", ",".join(subreddits)]
    if mock:
        cmd.append("--mock")
    workdir = ensure_private_dir(data_dir() / "vendor" / "last30days-work")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir, env=_env())
    except subprocess.TimeoutExpired as e:
        raise Last30Error(f"last30days timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise Last30Error(f"last30days exited {proc.returncode}: {proc.stderr.strip()[-300:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise Last30Error("last30days returned no JSON") from e


def items(export: dict) -> list[dict]:
    """Flatten to the fields we use. Text stays untrusted: only gtm/llm.py reads it.

    Author facts (pinned v3.30.0, `--json-profile raw`): HN items carry the poster's username in
    `author`; Reddit items carry none (the engine's Reddit normaliser sets author=None, and the
    raw export drops None fields), so `author` is None for Reddit. The HN discussion page is
    `metadata.hn_url` (`url` may be the linked article)."""
    out = []
    for source, rows in (export.get("items_by_source") or {}).items():
        for r in rows or []:
            if not r.get("url"):
                continue
            meta = r.get("metadata") or {}
            out.append({
                "source": source,
                "url": r["url"],
                "title": (r.get("title") or "")[:300],
                "text": (r.get("body") or r.get("snippet") or "")[:600],
                "own_text": _own_text(r.get("body") or "", meta.get("top_comments"))[:600],
                "posted_at": r.get("published_at"),
                "container": r.get("container"),
                "engagement": (r.get("engagement") or {}).get("score"),
                "author": (str(r["author"]).strip() or None) if r.get("author") else None,
                "discussion_url": meta.get("hn_url") if isinstance(meta.get("hn_url"), str) else None,
            })
    return out


def _own_text(body: str, comments) -> str:
    """The poster's own words: the engine appends the top 3 comments' excerpts (space-joined) to
    `body`; those are other people's words, so they are cut before intent labelling."""
    tail = " ".join(str(c.get("excerpt") or "").strip() for c in (comments or [])[:3] if isinstance(c, dict))
    body = body.strip()
    if tail.strip() and body.endswith(tail):
        body = body[:-len(tail)].rstrip()
    return body
