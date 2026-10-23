"""wigolo from Python: an MCP stdio client (`npx -y wigolo`), for scheduled runs (plan §1.4).

Interactive skills use the wigolo MCP tools directly; `gtm run` has no Claude Code session, so it
spawns the same server itself. One server process per `Wigolo()` context; reuse it for a batch.
Everything wigolo returns is untrusted web text: it only ever goes to gtm/llm.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOL_TIMEOUT_S = 120


class WigoloError(Exception):
    pass


def _server() -> StdioServerParameters:
    npx = os.environ.get("GTM_NPX") or shutil.which("npx")
    if not npx:
        raise WigoloError("npx not found: install Node (nvm) so wigolo can run")
    node_bin = str(Path(npx).resolve().parent)
    env = {"PATH": f"{node_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
           "HOME": os.environ.get("HOME", str(Path.home()))}
    for key in ("WIGOLO_SEARCH", "WIGOLO_DATA_DIR", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return StdioServerParameters(command=npx, args=["-y", "wigolo"], env=env)


def _decode(result) -> dict | list | str:
    if getattr(result, "isError", False):
        text = " ".join(getattr(c, "text", "") for c in result.content)
        raise WigoloError(text[:300] or "wigolo tool error")
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def call_many(calls: list[tuple[str, dict]]) -> list:
    """Run several tool calls on one server process. Each failure is returned as a WigoloError."""
    out: list = []
    with open(os.devnull, "w") as devnull:
        async with stdio_client(_server(), errlog=devnull) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for name, args in calls:
                    try:
                        res = await asyncio.wait_for(session.call_tool(name, args), TOOL_TIMEOUT_S)
                        out.append(_decode(res))
                    except (WigoloError, TimeoutError) as e:
                        out.append(WigoloError(f"{name}: {e}"))
    return out


def call(name: str, args: dict):
    if os.environ.get("GTM_FAKE") == "1":
        from gtm import fake
        return fake.wigolo(name, args)
    res = asyncio.run(call_many([(name, args)]))[0]
    if isinstance(res, Exception):
        raise res
    return res


def search(query: str | list[str], **kw) -> dict:
    return call("search", {"query": query, **kw})


def fetch(url: str, **kw) -> dict:
    return call("fetch", {"url": url, **kw})


def diff(old: dict, new: dict, **kw) -> dict:
    """old/new: {"markdown": ...} or {"url": ...} (wigolo's diff contract)."""
    return call("diff", {"old": old, "new": new, **kw})


def crawl(url: str, **kw) -> dict:
    return call("crawl", {"url": url, **kw})


# ---------------------------------------------------------------- install (plan §15.3)

MCP_ENTRY = {"command": "npx", "args": ["-y", "wigolo"]}


def warm() -> dict:
    """Start wigolo once so `npx -y` downloads and caches it before the first scheduled run."""
    page = fetch("https://example.com", max_content_chars=200)
    return {"ok": bool(page.get("markdown"))}


def register_project(mcp_json: Path, *, already_listed: bool | None) -> str:
    """Make wigolo available to Claude Code in this project. Never overwrites another server or a
    user-level wigolo the founder already has (that one may carry his own settings)."""
    if already_listed:
        return "already available"
    data = json.loads(mcp_json.read_text()) if mcp_json.exists() else {}
    servers = data.setdefault("mcpServers", {})
    if "wigolo" in servers:
        return "already in .mcp.json"
    servers["wigolo"] = dict(MCP_ENTRY)
    mcp_json.write_text(json.dumps(data, indent=2) + "\n")
    return "added to .mcp.json (Claude Code asks once to approve it)"
