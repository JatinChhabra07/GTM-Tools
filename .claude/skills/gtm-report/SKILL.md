---
name: gtm-report
description: Explains how the GTM outreach automation is doing, in plain language. It reads the latest run report, the lead funnel, reply rates with confidence intervals (overall, per persona, per segment, per discovery engine, and graph-reviewed vs. holdout leads), costs, items that need a human, and data-backed improvement proposals, then says what to do next. Use it whenever the founder asks "how is it going", "any replies", "what happened today", "show results", "is it working", "how much did it cost", "which segment works best", "which engine works", "is lookalike / social / ICP search finding good leads", or after a scheduled run.
---

# GTM report: how it's going

Reports are aggregates by design: no email bodies, addresses or reply text pass through this
session. That's on purpose (scraped and reply text is untrusted, and personal data stays out of
chat). If the founder wants to read emails or replies, that's `bin/gtm review` in their terminal.

Start with `bin/gtm product show --product <slug>` and say which product you're on.

## 1. Gather

- `bin/gtm report --product <slug>`: the latest run (status, counts, funnel, qualified leads,
  spend vs. budget). `--run <id>` for an older one.
- `bin/gtm report --product <slug> --outcomes`: results since the start.
- `bin/gtm report --product <slug> --engines [--days 30]`: the funnel per discovery engine (when
  the founder asks which way of finding companies works, or once there are replies).
- `bin/gtm manual list --product <slug>`: things waiting for a human.
- `bin/gtm report --product <slug> --proposals`: open improvement proposals.

## 2. Explain (short, plain words, most important first)

1. **Needs you**: emails waiting for review (`pending_review`) → `bin/gtm review --product <slug>`
   in their terminal; reply drafts → `bin/gtm review --product <slug> --drafts`; manual actions,
   each in one line with what to do.
2. **Results**: positive replies per 100 delivered, with the 95% range (`ci95_per_100`). Say plainly
   when the sample is too small: under ~50 delivered, the range is wide, so don't call a winner.
3. **Is the lead filter earning its keep?** Compare `graph_kept` vs. `holdout` (holdout leads skip
   the graph reviewers on purpose). Only call a difference when the ranges don't overlap.
4. **Health**: bounce rate (above 3% pauses the mailbox automatically), opt-out rate, and review
   rejections (`rejected_share` above ~30% means the writer needs better facts or proof:
   suggest `/gtm-onboard`).
5. **Funnel**: where leads drop (posts → intent → accounts → people → emails → qualified →
   written). Name the narrowest stage and the skill that fixes it.
6. **Cost**: Apify dollars and Snov credits per run vs. the caps.
7. **Which engine is working** (`--engines`, also a short table in each run report). Companies
   are found in up to five ways: `icp` (web search for the ICP), `lookalike` (companies like the
   clients or dream customers), `social` (people's posts and job ads), `competitor` (a
   competitor's customers), `manual` (lists the founder gave). For each, say in one line how many
   companies it found and how far they got: shortlisted → enriched → qualified → contacted →
   replied → positive. Then:
   - Say that a company found by two engines counts for both (the `note`), so rows overlap.
   - `too_few_to_tell: true` (under 50 delivered) means don't judge that engine yet. Only name a
     winner when `leader` is set (the ranges don't overlap); otherwise say "no clear winner yet"
     and why, even if one engine's rate looks higher.
   - An engine that finds companies but none survive the shortlist is a quality problem (route:
     `/gtm-icp` for social and ICP search, `/gtm-clients` for lookalikes). One that finds nothing
     is a volume problem. `lookalike` with a "no clients yet" note: suggest dream customers in
     `/gtm-clients`.
   - Never switch an engine off or change targeting yourself: at most suggest where to put more
     effort and let the founder decide (see Proposals).

## 3. Proposals

Each proposal is a pattern found in real reply data (only after ≥100 sends and ≥5 positive replies
in a segment). Present it with its numbers and ask the founder whether to act on it. If yes, make
the change with `/gtm-icp` (segments, personas, weights), then mark it:
`bin/gtm report --product <slug> --proposals --close <id>` (or `--close <id> --no-accepted` to
decline). Never change targeting without the founder's yes.

A proposal with `kind: engine` means one discovery engine's companies reply clearly better (past
the same evidence bar). It's a suggestion to shift effort: e.g. more clients or dream customers
for `lookalike` (`/gtm-clients`), more keywords or sources for `social` and more segments for
`icp` (`/gtm-icp`). Nothing is switched off automatically.

## 4. End with one clear next step.
