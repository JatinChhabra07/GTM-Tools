"""Per-product knowledge base (plan §2.2): mem0 OSS + Qdrant server + a local embedder.

kb.py is the only writer. Rules enforced here, not in prompts:
- infer=False on every add: mem0 never rewrites or deletes memories itself, so no LLM key is needed
- one Qdrant collection per product (gtm_<slug>) AND user_id=<slug> on every call
- no personal data (emails/phones rejected); lead-derived memories carry lead_key for erasure
- authority='human' memories (facts.yaml) change only through reload_facts()
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import warnings
from pathlib import Path

import httpx
import yaml

from gtm.paths import check_slug, ensure_private_dir, product_config_dir, product_data_dir
from gtm.util import has_pii, normalize_text, sha1

CATEGORIES = ("product_fact", "proof", "icp", "client_pattern", "competitor", "pain_language",
              "learning", "voice")
QDRANT_URL = os.environ.get("GTM_QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_IMAGE = "qdrant/qdrant:v1.20.0"  # matches qdrant-client 1.20
QDRANT_CONTAINER = "gtm-qdrant"
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIMS = 384


class KBError(Exception):
    pass


class HumanFactLocked(KBError):
    """Human facts (facts.yaml) can only change through `gtm kb reload-facts`."""


# ---------------------------------------------------------------- Qdrant server

def qdrant_ready(timeout: float = 2) -> bool:
    try:
        return httpx.get(f"{QDRANT_URL}/readyz", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_qdrant(wait_s: int = 30) -> str:
    """Start the Qdrant container if it isn't answering. Embedded Qdrant is not allowed: it takes
    an exclusive per-process lock, and scheduled runs + interactive skills run concurrently."""
    if qdrant_ready():
        return "running"
    docker = shutil.which("docker")
    if not docker:
        raise KBError("docker not found: install Docker so the knowledge base can run")
    from gtm.paths import data_dir
    storage = ensure_private_dir(data_dir() / "qdrant")
    exists = subprocess.run([docker, "inspect", QDRANT_CONTAINER], capture_output=True).returncode == 0
    if exists:
        cmd = [docker, "start", QDRANT_CONTAINER]
    else:
        cmd = [docker, "run", "-d", "--name", QDRANT_CONTAINER, "--restart", "unless-stopped",
               "-p", "127.0.0.1:6333:6333", "-v", f"{storage}:/qdrant/storage", QDRANT_IMAGE]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise KBError(f"could not start Qdrant: {proc.stderr.strip()[:300]}")
    for _ in range(wait_s):
        if qdrant_ready():
            return "started"
        time.sleep(1)
    raise KBError("Qdrant container started but is not answering on 6333")


# ---------------------------------------------------------------- backends

class _Mem0Backend:
    """Thin adapter so tests can swap in an in-memory backend."""

    def __init__(self, slug: str):
        pdir = ensure_private_dir(product_data_dir(slug) / "mem0")
        # read by mem0 at import time: set before the first import in this process
        os.environ["MEM0_TELEMETRY"] = "False"
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("MEM0_DIR", str(ensure_private_dir(product_data_dir(slug) / ".mem0")))
        warnings.filterwarnings("ignore", message="Cannot enable progress bars")
        ensure_qdrant()
        from mem0 import Memory
        self.slug = slug
        self.mem = Memory.from_config({
            "vector_store": {"provider": "qdrant", "config": {
                "collection_name": f"gtm_{slug}", "url": QDRANT_URL,
                "embedding_model_dims": EMBED_DIMS}},
            "embedder": {"provider": "huggingface", "config": {"model": EMBED_MODEL,
                                                               "embedding_dims": EMBED_DIMS}},
            # never called (infer=False everywhere); mem0 still constructs a client at init
            "llm": {"provider": "openai", "config": {"api_key": "unused-infer-false"}},
            "history_db_path": str(pdir / "history.db"),
        })

    def add(self, text: str, metadata: dict) -> str:
        res = self.mem.add([{"role": "user", "content": text}], user_id=self.slug,
                           metadata=metadata, infer=False)
        return res["results"][0]["id"]

    def search(self, query: str, filters: dict, k: int) -> list[dict]:
        res = self.mem.search(query, top_k=k, filters={"user_id": self.slug, **filters})
        return [_flatten(r) for r in res["results"]]

    def all(self, filters: dict, k: int = 1000) -> list[dict]:
        res = self.mem.get_all(filters={"user_id": self.slug, **filters}, top_k=k)
        return [_flatten(r) for r in res["results"]]

    def delete(self, memory_id: str) -> None:
        self.mem.delete(memory_id)


def _flatten(r: dict) -> dict:
    meta = dict(r.get("metadata") or {})
    return {"id": r["id"], "text": r.get("memory", ""), "score": r.get("score"), **meta}


class MemoryBackend:
    """In-memory stand-in with the same contract (tests, GTM_FAKE=1). Search = word overlap."""

    _stores: dict[str, list[dict]] = {}

    def __init__(self, slug: str):
        self.slug = slug
        self.rows = MemoryBackend._stores.setdefault(slug, [])

    def add(self, text: str, metadata: dict) -> str:
        mid = sha1(self.slug, text, str(len(self.rows)))
        self.rows.append({"id": mid, "text": text, **metadata})
        return mid

    def _match(self, row: dict, filters: dict) -> bool:
        return all(row.get(k) == v for k, v in filters.items())

    def search(self, query: str, filters: dict, k: int) -> list[dict]:
        q = set(normalize_text(query).split())
        hits = []
        for row in self.rows:
            if self._match(row, filters):
                overlap = len(q & set(normalize_text(row["text"]).split()))
                if overlap:
                    hits.append({**row, "score": overlap / max(len(q), 1)})
        return sorted(hits, key=lambda r: -r["score"])[:k]

    def all(self, filters: dict, k: int = 1000) -> list[dict]:
        return [dict(r) for r in self.rows if self._match(r, filters)][:k]

    def delete(self, memory_id: str) -> None:
        self.rows[:] = [r for r in self.rows if r["id"] != memory_id]


_open: dict[str, object] = {}


def backend(slug: str):
    slug = check_slug(slug)
    if slug not in _open:
        fake = os.environ.get("GTM_FAKE") == "1" or os.environ.get("GTM_KB_BACKEND") == "memory"
        _open[slug] = MemoryBackend(slug) if fake else _Mem0Backend(slug)
    return _open[slug]


# ---------------------------------------------------------------- public API

def _dedupe_key(category: str, text: str) -> str:
    return sha1(category, normalize_text(text))


def add(slug: str, category: str, text: str, *, source_url: str | None = None,
        authority: str = "auto", lead_key: str | None = None, extra: dict | None = None) -> dict:
    """Add one memory. Returns {"id", "status": "added"|"duplicate"}."""
    if category not in CATEGORIES:
        raise KBError(f"unknown category {category!r}; use one of {', '.join(CATEGORIES)}")
    text = (text or "").strip()
    if not text:
        raise KBError("empty memory")
    if len(text) > 2000:
        raise KBError("memory too long (2000 chars max): store one fact per memory")
    if has_pii(text):
        raise KBError("memory looks like it contains an email or phone number: no personal data in the KB")
    if category == "proof" and not source_url:
        raise KBError("a proof needs its source_url")
    if authority not in ("auto", "human", "site"):
        raise KBError("authority must be auto, site or human")
    be = backend(slug)
    key = _dedupe_key(category, text)
    existing = be.all({"dedupe": key}, k=1)
    if existing:
        return {"id": existing[0]["id"], "status": "duplicate"}
    meta = {"category": category, "authority": authority, "dedupe": key}
    if source_url:
        meta["source_url"] = source_url
    if lead_key:
        meta["lead_key"] = lead_key
    if extra:
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return {"id": be.add(text, meta), "status": "added"}


def search(slug: str, query: str, *, category: str | None = None, k: int = 5) -> list[dict]:
    filters = {"category": category} if category else {}
    return backend(slug).search(query, filters, k)


def all_memories(slug: str, *, category: str | None = None) -> list[dict]:
    return backend(slug).all({"category": category} if category else {})


def delete(slug: str, memory_id: str, *, _allow_human: bool = False) -> None:
    be = backend(slug)
    row = next((r for r in be.all({}) if r["id"] == memory_id), None)
    if row is None:
        raise KBError(f"no memory {memory_id}")
    if row.get("authority") == "human" and not _allow_human:
        raise HumanFactLocked("human facts change only via facts.yaml + `gtm kb reload-facts`")
    be.delete(memory_id)


def forget_lead(slug: str, lead_key: str) -> int:
    """Erasure: drop every memory derived from this lead."""
    be = backend(slug)
    rows = be.all({"lead_key": lead_key})
    for r in rows:
        be.delete(r["id"])
    return len(rows)


def facts_path(slug: str) -> Path:
    return product_config_dir(slug) / "facts.yaml"


def reload_facts(slug: str) -> dict:
    """The ONLY path that changes human facts: drop them all, re-add facts.yaml verbatim."""
    be = backend(slug)
    removed = 0
    for r in be.all({"authority": "human"}):
        be.delete(r["id"])
        removed += 1
    path = facts_path(slug)
    facts = (yaml.safe_load(path.read_text()) or {}).get("facts", []) if path.exists() else []
    added = 0
    for f in facts:
        res = add(slug, f["category"], f["text"], source_url=f.get("source_url"), authority="human")
        added += res["status"] == "added"
    return {"removed": removed, "added": added}


def ingest(slug: str, items: list[dict], *, authority: str = "site") -> dict:
    """Bulk add extracted facts ([{category, text, source_url?}]). Bad items are reported, not fatal."""
    out = {"added": 0, "duplicate": 0, "rejected": []}
    for i, item in enumerate(items):
        try:
            res = add(slug, item["category"], item["text"], source_url=item.get("source_url"),
                      authority=authority)
            out[res["status"]] += 1
        except (KBError, KeyError) as e:
            out["rejected"].append({"index": i, "reason": str(e)})
    return out


def extracted_path(slug: str) -> Path:
    return product_config_dir(slug) / "knowledge" / "extracted.jsonl"


def rebuild(slug: str) -> dict:
    """Re-create the KB from files: knowledge/extracted.jsonl + facts.yaml + learnings.jsonl."""
    be = backend(slug)
    for r in be.all({}):
        be.delete(r["id"])
    out = {"site": {}, "human": {}, "learning": {}}
    ex = extracted_path(slug)
    if ex.exists():
        out["site"] = ingest(slug, [json.loads(line) for line in ex.read_text().splitlines() if line.strip()])
    out["human"] = reload_facts(slug)
    learn = product_config_dir(slug) / "learnings.jsonl"
    if learn.exists():
        items = [json.loads(line) for line in learn.read_text().splitlines() if line.strip()]
        out["learning"] = ingest(slug, [{"category": "learning", **i} for i in items], authority="auto")
    return out


def stats(slug: str) -> dict:
    rows = backend(slug).all({})
    by_cat: dict[str, int] = {}
    for r in rows:
        by_cat[r.get("category", "?")] = by_cat.get(r.get("category", "?"), 0) + 1
    return {"total": len(rows), "by_category": by_cat,
            "human": sum(1 for r in rows if r.get("authority") == "human")}
