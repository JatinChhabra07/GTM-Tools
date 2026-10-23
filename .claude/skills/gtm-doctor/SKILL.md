---
name: gtm-doctor
description: Health check for the GTM-Tools outreach automation. Checks that every tool it depends on (isolated Claude calls, wigolo MCP, graph-engineering and human-voice skills, last30days, Docker/Qdrant, Apify and Snov keys) is installed and working, and explains any problem in plain language with the fix. Use it when the founder asks "is everything working", "why didn't it run", "check the setup", before a first run, or whenever a /gtm-* command fails with a tool or key error.
---

# GTM doctor

1. Run `bin/gtm doctor` (add `--product <slug>` if a product exists; `bin/gtm product list`
   shows them). It prints JSON: each check has `ok` (true / false / null = couldn't tell) and
   what that tool is used for.
2. Check that the wigolo MCP really answers, not just that it's listed: call its `cache` tool with
   `stats: true` (free, local). If it fails, say so.
3. Report in plain language. Start with what's broken and how to fix it, then one line saying
   what's fine. Fixes:
   - `claude_isolation` false: run `claude update` (the CLI lacks a safety flag; the system
     refuses to make unsafe AI calls).
   - `signing_keys` false: the founder runs `bin/gtm keys init` in his own terminal.
   - `apify_key` / `snov_keys` false: the founder runs `bin/gtm secrets set` in his own terminal.
   - `wigolo_mcp` false: wigolo isn't registered in Claude Code (`claude mcp list`).
   - `last30days_engine`, `docker`: needed from Phase 0C; not a problem before then.
   - `data_dir` / `config_dir` on `/mnt/windows`: data must live on the Linux disk; check
     the `GTM_DATA_DIR` / `GTM_CONFIG_DIR` environment variables.
4. Never print or ask for secret values; `doctor` only shows whether keys are set.
