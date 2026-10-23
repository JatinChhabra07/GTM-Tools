---
name: gtm-accounts
description: Builds and maintains the Target Account List for the GTM outreach automation, i.e. the companies worth watching. It searches the web for companies matching the ICP and lookalikes of existing clients (free, via wigolo), profiles their websites, optionally buys LinkedIn company data after showing the cost, pre-scores and tiers every account, and imports any target list the founder already has. Use it after /gtm-icp and /gtm-clients, or when the founder says "find companies", "build my target list", "find companies like Snapdeal", "add these companies", "we're running out of leads", or asks which companies are being tracked.
---

# GTM accounts: the target company list

Runs only look for signals at companies on this list (plus companies that show up through a
named person's post), so its quality caps everything downstream. Pre-scores rank accounts by ICP
fit and client lookalike; the scheduled runs then add intent and timing.

Start with `bin/gtm product show --product <slug>` and say which product you're on.

## 1. Preconditions

`bin/gtm config get --product <slug>` must show at least one ICP segment, or clients must be
loaded. If neither: say so and route to `/gtm-icp` or `/gtm-clients`.

## 2. Build (free part first)

`bin/gtm accounts build --product <slug> --estimate`

It searches with ICP and lookalike queries (wigolo, free), lets an isolated Claude call classify
which results are real company sites (directories, listicles and competitors are dropped),
profiles each new company's website (free), pre-scores everything, and reports
`without_linkedin_data` and `linkedin_data_estimate_usd`.

Then ask the founder (AskUserQuestion): buy LinkedIn company data for N companies at ~$X (better
industry / size / country, so better pre-scores), or stay free? If yes:
`bin/gtm accounts build --product <slug> --confirm`. It only buys for companies that don't have
the data yet, capped by `tal.build_budget_usd`.

`--max-new <n>` (default 60) caps new companies per call; the list stops growing at
`tal.max_accounts` (default 500).

## 3. Founder's own list (optional)

If the founder already has target companies, write them to a temp CSV `company,domain` and run
`bin/gtm accounts import --product <slug> --file <csv>`. They're profiled and pre-scored like the
rest.

## 4. Review the list with the founder

`bin/gtm accounts list --product <slug> --limit 30` shows the top accounts with `prescore`,
`tal_tier` (1 best) and `status` (`prequalified` = above the cutoff, `below_cutoff`, `client`,
`near_miss`). Show the top 10–15 as a short table and ask: do these look like companies you'd sell
to? If many look wrong, the fix is upstream: segments in `/gtm-icp` or clients in `/gtm-clients`,
then rebuild.

If clients are loaded and the list now has 20+ accounts, run
`bin/gtm accounts calibrate --product <slug>` and explain the result as `/gtm-clients` step 3 does.

## 5. Finish

Summarize: accounts found, how many prequalified, money spent (if any), and the next step: a first
run with `/gtm-run`. After that, the scheduled runs keep the list fresh (weekly re-profiling, new
companies from signals).
