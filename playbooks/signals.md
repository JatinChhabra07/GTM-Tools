# Signals playbook

How to turn a confirmed signal into the reason for an email. The writer gets ONE primary signal
(highest rank) and one proof. Adapted, with credit, from Harvey's `skills/signal_playbook.md`
(MIT, see `NOTICE`); signal codes are the ones in `gtm/catalog.py` (plan §5.1).

## The core rule

The signal is the reason you're emailing today. Say it plainly in the first sentence, as an
observation. Never dress it up and never claim more than the signal proves. If the only "signal"
is that they match the ICP, you don't have one: open with a true problem their role or industry
faces ("most support teams at your size still triage by hand") and keep it honest.

A wrong specific is worse than a right generic. A four-month-old job post that is now filled is
not "you're hiring".

## Signal rank

Use exactly one signal as the hook, chosen in this order:

1. Stated intent (`ASKS_FOR_SOLUTION`, `COMPETITOR_PAIN`, `SWITCHING`)
2. Hiring (`HIRING_FOR_PROBLEM`, `HIRING_ROLE`, `EXEC_POST_TOPIC`)
3. Funding and leadership change (`FUNDING`, `LEADERSHIP_CHANGE`)
4. Tech change (`TECH_ADDED`, `TECH_REMOVED`, `PRICING_CHANGE`)
5. Tech present and ad spend (`TECH_STACK`, `RUNNING_GOOGLE_ADS`, `RUNNING_META_ADS`)
6. Industry problem, when there is no real signal

Stacking two signals in one email reads like a dossier. Keep the second for email 2.

## Freshness

A signal older than its window is never used as the hook, and the lead is re-checked at push time.

| Signal | Usable for |
|---|---|
| `ASKS_FOR_SOLUTION` | 14 days |
| `COMPETITOR_PAIN` | 21 days |
| `SWITCHING` | 21 days |
| `EXEC_POST_TOPIC` | 21 days |
| `HIRING_FOR_PROBLEM` | 45 days, or while the post is live |
| `HIRING_ROLE` | 45 days, or while the post is live |
| `FUNDING` | 60 days |
| `LEADERSHIP_CHANGE` | 90 days |
| `LAUNCH` | 45 days |
| `EXPANSION` | 60 days |
| `SITE_CHANGE` | 30 days |
| `PRICING_CHANGE` | 30 days |
| `TECH_ADDED` | 30 days |
| `TECH_REMOVED` | 30 days |
| `RUNNING_GOOGLE_ADS` | 30 days |
| `RUNNING_META_ADS` | 30 days |

## Stated intent

Someone named asked for a tool, complained about a competitor, or said they're switching. This
is the strongest hook, and the most sensitive. Follow the reference policy in `writing.md`:
if the lead wrote it, reference the topic, not the act of reading it. If a colleague wrote it,
use the pain theme only. Never quote it back at length.

## Hiring

The angle is the workload behind the hire, not the hire itself. A company hiring a second SDR
usually has reply volume the first one can't handle; ask about that. Only name the role if the
evidence names it. "Hiring in support" is fine; inventing "hiring three agents" is not.

## Funding

Everyone emails a freshly funded company, so the bar for specificity is higher. Talk about the
pressure the raise creates (a bigger target, fast hiring, a new market), not the raise itself.
Never open with a celebration line.

## Leadership change

A new leader in our buyer's function usually reviews tools in their first 90 days. Ask what
they're changing first; don't assume they inherited a problem.

## Tech change and tech present

Use it to place the product next to what they already run, or to point at a visible gap ("no
chat widget on the pricing page"). Name at most one tool, and only if it carries the point.
Listing their stack back at them reads as scraped.

## Ad spend

`RUNNING_GOOGLE_ADS` or `RUNNING_META_ADS` means they pay for traffic now. The honest angle is
what happens to that traffic after the click. Don't guess their budget.

## No real signal

Be honest about it. A true observation about their role or industry beats a fake personal line.
Flattery about their website is not a signal.
