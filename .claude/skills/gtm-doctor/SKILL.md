---
name: gtm-doctor
description: Health check for the GTM-Tools outreach automation. Checks that every tool it depends on (isolated Claude calls, wigolo, mem0/Qdrant knowledge base, last30days, graph-engineering and human-voice skills, Docker, Apify, Snov and Firecrawl keys, the sending mailbox and the schedule) is installed and working, and explains each problem in plain language with the exact fix. Use it when the founder asks "is everything working", "why didn't it run", "check the setup", before a first run, or whenever a /gtm-* command or scheduled run fails with a tool, key or connection error.
---

# GTM doctor

1. `bin/gtm doctor` (add `--product <slug>` when a product exists; `bin/gtm product list` shows
   them). JSON: each check has `ok` (true / false / null = couldn't tell) and what it's used for.
2. Live checks, all free: `bin/gtm tools check` (one wigolo search, fetch and diff from Python, and
   one last30days run, ~1–3 minutes), and when keys are set `bin/gtm tools check --paid` (reads
   the Apify month spend and the Snov balance; spends nothing).
3. Check that the wigolo MCP answers in this session too: call its `cache` tool with
   `stats: true`.
4. For one product: `bin/gtm setup status --product <slug>` (`progress`: KB facts, signals,
   accounts, mailboxes, Snov link, schedule) and `bin/gtm manual list --product <slug>`.
5. Report: what's broken and its fix first, then one line on what's fine. Fixes:
   - `claude_isolation` false: run `claude update` (the CLI lacks a safety flag; the system
     refuses to make unsafe AI calls).
   - `docker` false: Docker isn't installed or the user isn't in the `docker` group. That needs the
     founder's sudo password, so give the command for their terminal:
     `sudo usermod -aG docker $USER`, then log out and back in.
   - `qdrant` false: `bin/gtm kb up` (starts the knowledge-base database). If it keeps failing,
     `docker ps -a` shows whether the `gtm-qdrant` container exists.
   - `last30days_engine` false: `bin/gtm tools install`.
   - `signing_keys` false: the founder runs `bin/gtm keys init` in their own terminal.
   - `apify_key` / `snov_keys` false, or `--paid` shows `ok: false`: the founder runs
     `bin/gtm secrets set` in their own terminal (a 401 means the key is wrong or revoked).
   - `wigolo_mcp` false: wigolo isn't registered in Claude Code (`claude mcp list`). Scheduled
     runs start it themselves with `npx -y wigolo`, so check Node via nvm is available.
   - `graph_engineering_skill` / `human_voice_skill` false: the skills are missing from
     `~/.claude/skills/`. The pipeline has their rules built in, so runs still work; only manual
     deep reviews lose them.
   - mailbox NOT READY: `bin/gtm warmup <sending-domain>` lists the failing DNS checks.
   - `data_dir` / `config_dir` on `/mnt/windows`: data must live on the Linux disk; check the
     `GTM_DATA_DIR` / `GTM_CONFIG_DIR` environment variables.
   - a scheduled run didn't happen: `bin/gtm schedule show --product <slug>`; the cron log is in
     the product's data folder under `cron/` (the founder can open it; it holds no secrets).
6. Never print or ask for secret values; `doctor` only shows whether keys are set.
