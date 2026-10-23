---
name: gtm-run
description: Runs the GTM outreach pipeline once, on demand, and explains the result. It collects buying signals (LinkedIn/X/Instagram posts, hiring, funding, website changes, Reddit pain language via last30days), finds the right people and verified emails, filters to the best 10–15 leads with the graph-engineering reviewers, and writes emails that wait for the founder's approval. Use it when the founder says "run it now", "find leads", "get me leads today", "test the pipeline", "do a dry run", "check replies", or wants to see the automation work before scheduling it. Never sends email itself.
---

# GTM run: one pipeline run on demand

The scheduled runs (`/gtm-setup` installs them) do exactly this unattended. Running it by hand is
for a first test, an extra run, or debugging. A run **never sends email**: it ends with emails
waiting in the founder's review queue, and only `bin/gtm review` (their terminal, their PIN)
approves them.

Start with `bin/gtm product show --product <slug>` and say which product you're on.

## 1. Pick the mode

| mode | what it does | cost |
|---|---|---|
| `noop` | health check: one isolated Claude call, one wigolo call, one database write | free |
| `refresh` | lighter signal pass (LinkedIn only, fewer posts) → people → score → write | small |
| `full` | everything: LinkedIn, X, Instagram, jobs, news, site changes, Reddit/HN pain language → people → emails → graph review → write | up to the per-run budget |
| `sync` | pull replies, opt-outs and bounces from Snov; push already-approved emails | Snov API only |

First time ever: `noop`, then `full`. If the founder only asked "check replies": `sync`.
Before a `full` or `refresh`, say what the budget caps are (`bin/gtm config get` → `budgets`; default
$3 Apify and 60 Snov credits per run). Runs cannot spend past them.

## 2. Run

`bin/gtm run --product <slug> --mode <mode>`

A full run takes 5–30 minutes (Apify scrapers have to finish). Run it in the background
(`run_in_background`) and tell the founder it's working; don't poll. Before a mailbox is READY,
runs cap new leads at 10 a day (`--limit` changes that).

If it says another run is in progress, a scheduled run is already going. Wait for it rather than
starting a second one.

## 3. Explain the result

The command prints JSON: `status` (`done`, `partial`, `failed`), `reason`, `counts`, `notes`, and
the report path. Then show `bin/gtm report --product <slug>`: aggregates, the funnel, and the
qualified leads table (no email bodies or addresses, by design).

- `done` with `pending_review > 0`: tell the founder N emails are waiting and that they review them
  in their own terminal: `bin/gtm review --product <slug>`. You can't run it for them.
- `partial`: a budget, the call cap, or the Claude usage limit stopped it. That's safe: the next run
  resumes. Say which one (`reason`).
- `failed`: read `reason` and `notes`; use `/gtm-doctor` for tool or key errors.
- 0 leads: walk the funnel top-down and name the stage that dropped to 0 (no posts → keywords in
  `/gtm-icp`; no prequalified accounts → `/gtm-accounts`; no emails found → normal for some
  companies; all dropped by review → the report's drop reasons).

`bin/gtm manual list --product <slug>` shows anything the system needs a human to check (e.g. a
push to Snov that may or may not have gone through). Explain each item; the founder marks it done
with `bin/gtm manual done <id>` once handled.

## Never
- Never run `review`, `sending resume`, `autosend` or `mailbox add`. They're the founder's, and
  the CLI refuses them inside Claude Code anyway.
- Never open the SQLite files or `~/.local/share/gtm-tools/` directly; everything goes through
  `bin/gtm`.
