---
name: gtm
description: Front door of the GTM-Tools cold-outreach automation. Works out where the founder is (setup, product knowledge, targeting, clients, accounts, mailbox, schedule, daily review) and sends them to the right next step, or explains how the whole system works in plain words. Use it whenever the founder says "start", "help", "what's next", "what can you do", "how does this work", "start outreach", "get me customers", or anything about the outreach automation that doesn't clearly match a specific /gtm-* skill.
---

# GTM: where am I, what's next

## How the system works (say this when asked, in your own words, briefly)

1. **Learn the product**: reads the website into a knowledge base only this product uses.
2. **Decide who to target**: ideal companies, the people to email, signals worth acting on.
3. **Learn from clients**: finds companies that look like existing customers.
   **Learn from competitors**: a sourced brief per competitor, and the companies that use them.
4. **Watch for signals** two or three times a day: people asking for a solution or complaining
   about a competitor on LinkedIn/X, hiring, funding, website and pricing changes, plus
   Reddit/Hacker News for buyer language.
5. **Find the right person and a verified email** at each company that shows a signal.
6. **Keep only the best 10–15**: independent AI reviewers check every lead; weak ones are dropped.
7. **Write a short 3-email sequence** per lead, grounded only in real facts and the actual signal.
8. **The founder approves** each sequence with their PIN; only then does Snov.io send it, within
   safe daily limits. Replies, opt-outs and bounces are handled automatically.

## Route

Run `bin/gtm setup status` (with `--product <slug>` when one exists; ask which if several). Then:

| state | next step |
|---|---|
| tools or products missing, keys/PIN not set, sender or countries not set | `/gtm-setup` |
| no product knowledge (`kb_facts` 0) | `/gtm-onboard` |
| no personas, segments or confirmed signals | `/gtm-icp` |
| no clients loaded | `/gtm-clients` (optional but the biggest quality boost) |
| no target accounts | `/gtm-accounts` |
| competitors listed but never analysed, or founder asks about competitors | `/gtm-competitors` (optional) |
| no mailbox, Snov link or schedule | `/gtm-setup` (sending section) |
| all set, never run | `/gtm-run` (noop, then full) |
| running | `/gtm-report` for results, `bin/gtm review --product <slug>` (their terminal) for approvals |
| anything failing | `/gtm-doctor` |

Say the next step in one sentence and offer to start it. Don't run several skills in one go
unless the founder asks; each one asks its own questions.

## The founder's daily routine once running
- Morning: `bin/gtm review --product <slug>` in their terminal. Read, edit or approve the day's
  emails (one PIN entry).
- Any time: `/gtm-report` for how it's going.
- Emergency stop: `bin/gtm sending pause` (stops all sending at once; resuming needs their PIN).
