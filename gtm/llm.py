"""Quarantined LLM calls (plan §1.3, adapted from Harvey's claude_runtime.py, MIT).

Every model call that reads untrusted text (scraped posts, sites, replies) goes through here:
`claude -p` with no tools, no MCP servers, no skills/slash commands, no settings files, no
session saved, prompt on stdin, cwd = a fresh empty temp dir, and an environment stripped of
overrides. A prompt injection inside the text then has nothing to act with.

If the installed CLI lacks any isolation flag we refuse to call it at all; there is no
unisolated fallback.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

# Order matters only for readability; all are required.
REQUIRED_FLAGS = (
    "--tools",
    "--strict-mcp-config",
    "--safe-mode",
    "--setting-sources",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--output-format",
)

# Stripped so calls stay on the founder's Claude subscription and can't be redirected.
# ANTHROPIC_API_KEY stripping is our addition: with it set, claude would bill the API instead.
STRIPPED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CONFIG_DIR_OVERRIDE",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "MCP_CONFIG",
)

USAGE_LIMIT_MARKERS = ("usage limit", "rate limit", "limit reached", "quota", "overloaded")


class LLMError(Exception):
    pass


class IsolationUnavailable(LLMError):
    """The installed claude CLI can't be isolated; never call it unisolated."""


class UsageLimit(LLMError):
    """Subscription limit hit: callers pause LLM work and end the run `partial`."""


@dataclass
class CallResult:
    text: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    duration_ms: int = 0
    raw: dict = field(default_factory=dict)


def claude_bin() -> str:
    path = os.environ.get("GTM_CLAUDE_BIN") or shutil.which("claude")
    if not path:
        raise IsolationUnavailable("claude CLI not found on PATH")
    return path


_probe_cache: dict[str, tuple[str, ...]] = {}


def probe_isolation(binary: str | None = None) -> None:
    """Refuse unless `claude --help` lists every isolation flag (Harvey's help-probe)."""
    binary = binary or claude_bin()
    if binary not in _probe_cache:
        try:
            out = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=30,
                                 env=_clean_env()).stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            raise IsolationUnavailable(f"could not run `{binary} --help`: {e}") from e
        _probe_cache[binary] = tuple(f for f in REQUIRED_FLAGS if f not in out)
    missing = _probe_cache[binary]
    if missing:
        raise IsolationUnavailable(
            f"claude CLI lacks isolation flags {', '.join(missing)}: run `claude update`. "
            "Refusing to call it unisolated."
        )


def build_command(binary: str, model: str | None) -> list[str]:
    cmd = [
        binary, "-p",
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--safe-mode",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    env["CLAUDE_CODE_SAFE_MODE"] = "1"
    return env


def call(prompt: str, *, model: str | None = None, timeout: int = 300) -> CallResult:
    """One isolated, tool-less call. The prompt (including any untrusted text) goes on stdin."""
    binary = claude_bin()
    probe_isolation(binary)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="gtm-llm-") as cwd:
        try:
            proc = subprocess.run(build_command(binary, model), input=prompt, capture_output=True,
                                  text=True, timeout=timeout, cwd=cwd, env=_clean_env())
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude call timed out after {timeout}s") from e
    duration_ms = int((time.monotonic() - started) * 1000)
    return parse_output(proc.returncode, proc.stdout, proc.stderr, duration_ms)


def parse_output(returncode: int, stdout: str, stderr: str, duration_ms: int = 0) -> CallResult:
    blob: dict = {}
    try:
        blob = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        blob = {}
    message = " ".join(str(x) for x in (blob.get("result", ""), stderr) if x).lower()
    if returncode != 0 or blob.get("is_error"):
        if any(m in message for m in USAGE_LIMIT_MARKERS):
            raise UsageLimit(message[:300] or "usage limit")
        raise LLMError(f"claude exited {returncode}: {(stderr or stdout)[:300]}")
    if "result" not in blob:
        raise LLMError("claude returned no JSON result")
    usage = blob.get("usage") or {}
    models = list((blob.get("modelUsage") or {}).keys())
    return CallResult(
        text=blob["result"],
        model=models[0] if models else None,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        duration_ms=duration_ms,
        raw=blob,
    )


def untrusted(label: str, text: str, limit: int = 4000) -> str:
    """Wrap scraped/reply text as data. Strips control and zero-width chars and our own tags."""
    cleaned = "".join(ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and ch not in _ZERO_WIDTH))
    cleaned = cleaned.replace("</untrusted", "&lt;/untrusted").replace("<untrusted", "&lt;untrusted")
    return f'<untrusted source="{label}">\n{cleaned[:limit]}\n</untrusted>'


_ZERO_WIDTH = {"​", "‌", "‍", "⁠", "﻿", "‮", "‭"}
