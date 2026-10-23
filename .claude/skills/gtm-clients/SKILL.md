---
name: gtm-clients
description: Loads the founder's existing customers (and lost deals) into the GTM outreach automation so it can find lookalike companies, e.g. "Snapdeal uses us, so find companies like Snapdeal". It collects the client list in chat, profiles each client's website for free, optionally buys LinkedIn company data after showing the cost, stores client patterns in the product's knowledge base, and checks that the scoring ranks real clients above strangers. Use it when the founder mentions current clients, customers, case studies, "companies like X", "lookalikes", lost deals, or wants better-fit targets, even if they don't say "clients".
---

# GTM clients: the lookalike engine

Real clients are the best evidence of who buys. Their industries, sizes, countries and tech stacks
become the pattern new accounts are scored against. Near-misses (fit on paper, never bought) keep
the scoring honest by showing what *not* to over-reward.

Start with `bin/gtm product show --product <slug>` and say which product you're on. Client lists
belong to one product only.

## 1. Collect the lists (chat is fine: company names and websites aren't secrets)

Ask for 5–20 current clients: company name, website, and if the founder knows them, rough deal
size (any currency, a number) and months as a client. More is better; 3 is the minimum for the
calibration check. Then ask for 5–20 near-misses: companies that looked like a fit but never bought
(lost deals, churned in the trial, said no).

Don't ask for contact people or emails: this list is about companies.

Write `products/<slug>/clients.csv`:

```csv
company,domain,contract_size,retention_months,notes
Snapdeal,snapdeal.com,12000,18,enterprise plan
Brandly,brandly.co,2400,9,
```

and `products/<slug>/near_miss.csv` with `company,domain,notes`. Domains are bare (no `https://`,
no `www.`). If a file exists, merge: keep earlier rows unless the founder removes them. Both
files are git-ignored.

## 2. Import and profile (free)

`bin/gtm accounts clients --product <slug> --estimate` imports both lists and shows what
LinkedIn company data would cost (`linkedin_data_estimate_usd`). It improves industry, size and
HQ data, which the website alone often lacks.

Ask the founder (AskUserQuestion): buy it for ~$X, or continue free? Then run either
`bin/gtm accounts clients --product <slug>` (free: profiles their websites only) or
`... --confirm` (free profile + the paid LinkedIn data, capped at 1.5× the estimate).

Report: clients imported, skipped rows (and why: usually a missing domain), sites profiled,
`client_patterns` stored in the KB.

## 3. Calibrate

`bin/gtm accounts calibrate --product <slug>`. It is a leave-one-out test: is each client scored
as well as the best 20% of similar strangers?

- `passed: true`: say so in one line.
- `sample_size` under 20: normal before `/gtm-accounts` has built the target list. Tell the founder
  calibration becomes meaningful after that, and re-run it then.
- `clients_above_p80` under 0.8 with a real sample: the ICP doesn't describe the clients. Show which
  client facts disagree with the segments (`bin/gtm accounts list --product <slug>` shows each
  account's data) and suggest `/gtm-icp` to adjust segments.
- `near_miss_gap` under 15: near-misses score like clients, so something they share with clients
  isn't what makes people buy. Ask the founder what was actually different about the clients, and
  record the answer as a human fact via `/gtm-onboard` step 4.

## 4. Finish

Summarize: N clients and M near-misses loaded, the patterns found (top industries, size bands,
countries, tools), calibration status, and the next step: `/gtm-accounts`.
