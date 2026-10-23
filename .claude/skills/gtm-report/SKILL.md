---
name: gtm-report
description: Explains how the GTM outreach automation is doing, in plain language. It reads the latest run report, the lead funnel, reply rates with confidence intervals (overall, per persona, per segment, and graph-reviewed vs. holdout leads), costs, items that need a human, and data-backed improvement proposals, then says what to do next. Use it whenever the founder asks "how is it going", "any replies", "what happened today", "show results", "is it working", "how much did it cost", "which segment works best", or after a scheduled run.
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

## 3. Proposals

Each proposal is a pattern found in real reply data (only after ≥100 sends and ≥5 positive replies
in a segment). Present it with its numbers and ask the founder whether to act on it. If yes, make
the change with `/gtm-icp` (segments, personas, weights), then mark it:
`bin/gtm report --product <slug> --proposals --close <id>` (or `--close <id> --no-accepted` to
decline). Never change targeting without the founder's yes.

## 4. End with one clear next step.
