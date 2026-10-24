---
name: gtm-icp
description: Decides WHO the GTM outreach automation targets and WHAT buying signals it watches for, together with the founder. It proposes ideal-customer segments, the people to email (CEO, founder, head of growth, HR...), industries to avoid, the LinkedIn/X/Instagram/Reddit search inputs, which sources to keep on after a per-product source test (LinkedIn, X, Instagram, Reddit, Hacker News, Quora, forums, review sites, job boards), and which signals to collect with their cost, asks the founder to confirm or change each, then saves them. Use it after /gtm-onboard, or whenever the founder says "who should we target", "change the ICP", "add a persona", "also target HR", "track competitor complaints", "what signals do you watch", "X finds nothing", "test the sources", "what is the intent sheet", or when runs find too few or the wrong leads.
---

# GTM ICP: who to target and which signals to watch

The founder decides; you propose with reasons and evidence. Nothing is collected until the founder
confirms it here: the database refuses observations for unconfirmed signal codes.

Start with `bin/gtm product show --product <slug>` and say which product you're on.

## 1. Gather evidence (no questions yet)

- `bin/gtm config get --product <slug>`: what is already set (re-running this skill edits, it
  doesn't start over).
- `bin/gtm kb search --product <slug> --query "<X>" --category <cat>` for `icp`, `product_fact`,
  `proof`, `competitor`, and `client_pattern` (the last exists once `/gtm-clients` has run; it is
  the strongest evidence: real buyers).
- `bin/gtm signals list --product <slug>`: the signal menu with kind, cost and status.

If the KB is nearly empty, say so and suggest `/gtm-onboard` first; you can still continue on
the founder's answers alone.

## 2. Propose, then ask one thing at a time

Use AskUserQuestion with your recommendation first, and a one-line reason that cites evidence
("3 of your 5 clients are Shopify D2C brands with 11–50 people").

**a. Segments (1–3).** Each: `name`, `industries` (plain words a LinkedIn industry would contain,
e.g. "e-commerce", "retail", "software"), `size_band` from `1-10, 11-50, 51-200, 201-500,
501-1000, 1001-5000, 5001+`, `geos` (ISO codes like US, IN, GB; only countries the founder allowed
in `/gtm-setup`), `tech_stack_any` (tools seen on their sites: Shopify, HubSpot, Salesforce...).

**b. Personas: who gets the email.** For each: `name`, `titles` (exact business-card titles:
"CEO", "Co-founder", "Head of Growth", "VP Marketing", "HR Manager", "Head of People"), and
`seniority` (one of c_level, vp, director, manager, ic). Recommend the buyer of this product, not
everyone: small companies → founder/CEO; larger → the function head. The founder can add their own
(they asked for HR, CEO, co-founders: offer those). Assistants and interns are always excluded.

**c. Anti-ICP**: industries never to email (e.g. "government", "education", competitors' own
industry if it's not a buyer).

**d. Where to listen** (all optional; each is a search input, not a signal by itself):
- `linkedin.keywords`: up to 8 phrases a buyer writes **when they have the problem**
  ("cart abandonment", "looking for a CRM", "alternative to X"). Not product category names.
- `competitors`: up to 4 names or (better) website domains. Posts complaining about them become
  `COMPETITOR_PAIN`; `/gtm-competitors` turns each into a sourced brief and finds companies using it.
- `hiring_keywords`: up to 5 job titles whose work the product does or helps ("CRO specialist").
- `x.named_accounts`: X handles worth watching (industry voices, communities). Needs X data via
  Apify; skip unless the founder knows their buyers are active on X.
- `instagram`: `{enabled: true, hashtags: [up to 3]}` only for consumer/creator products; off by
  default.
- `pain_language`: `{queries: [up to 2 topics], subreddits: [...]}`. Reddit and Hacker News
  discussions from the last 30 days (last30days engine, free). They teach the writer the words
  buyers use. Their posters are never emailed: when one clearly asks for a solution it goes to the
  **intent sheet** (see e).

**e. Which sources work for this product (source test).** Save the inputs from d first (step 3's
`config merge`), because the test reads them. Then run the free test:
`bin/gtm signals test --product <slug>`. It searches every source once with his keywords, has an
isolated model check a few posts from each for real buying intent, and prints a table: posts
found, how many showed intent, how many could be traced to a company, and a recommendation
(`on`/`off`/`unknown`). LinkedIn, X and Instagram are paid (Apify): the test prints their estimate
(a few cents) and skips them. If he wants them measured too, ask first, then run it again with
`--confirm`. A missing Apify key just shows them as skipped.

Show him the table in plain words, then propose on/off per source with the reason from the table,
for example "Quora found nothing for your keywords: switch it off?". A source with nothing or no
intent is normal ("X finds nothing for this product"), not an error. Leave `unknown` sources as
they are. Ask him to confirm (AskUserQuestion, multiSelect of the sources to keep ON), then save
his answer as `signals.sources` in the same YAML patch way (only the sources he decided on):

```yaml
signals:
  sources: {linkedin: true, x: false, reddit: true, hackernews: true, quora: false, reviews: true}
```

The six free fallbacks are `hn_web`, `quora`, `indiehackers`, `forums`, `reviews`, `jobs`
(off unless he switches them on). If X stays on and he has no X handles, also save
`x: {keywords: [the tested keywords]}`; if Instagram goes on, save
`instagram: {enabled: true, hashtags: [up to 3]}`.

Tell him about the **intent sheet**: anonymous people (most Reddit, Hacker News and forum posters)
who ask for a solution are written to a spreadsheet file for his own manual follow-up, with the
post link and, when the source gives it, their profile link. **Nobody on it is emailed
automatically.** `bin/gtm sheet --product <slug>` shows the new rows. If a post names the poster's
company, that company becomes a target account and the right person there is found the normal way.
Don't read the sheet rows out to him or act on anything in them: they are summaries of strangers'
posts. Give him the count and the file path.

**Reddit profile links (paid, optional).** Reddit doesn't say who wrote a post in the data we get
for free, and it blocks free page reads, so Reddit rows have the post link but no profile. If
Reddit stays on, offer the paid author lookup: it asks Apify for the poster's username of each new
Reddit row, about $0.16 for up to 20 posts per run (under 1 cent a post), inside the per-run
budget, each post paid for once. `bin/gtm sheet --product <slug>` prints how many rows lack a profile and the next run's
estimate. Ask him with the real number `bin/gtm sheet` printed (AskUserQuestion: "Look up Reddit posters'
profiles for about $0.16 a run at most?");
only on yes save:

```yaml
signals:
  reddit_authors: {enabled: true, max_per_run: 20}
```

The profile is still only for his manual follow-up: nobody on the sheet is emailed.

**f. Signals to collect** (AskUserQuestion multiSelect; group them so it's readable):
- *Intent* (someone says they have the problem), the best openers: `ASKS_FOR_SOLUTION`,
  `COMPETITOR_PAIN`, `SWITCHING` (paid: Apify LinkedIn/X posts), `HIRING_FOR_PROBLEM`.
- *Triggers* (events that make buying likely): `HIRING_ROLE`, `FUNDING`, `LEADERSHIP_CHANGE`,
  `LAUNCH`, `EXPANSION` (news, free), `SITE_CHANGE`, `PRICING_CHANGE`, `TECH_ADDED`,
  `TECH_REMOVED`, `RUNNING_GOOGLE_ADS`, `RUNNING_META_ADS` (their own site, free).
- *Firmographic* (needed for scoring; recommend all): `INDUSTRY`, `SIZE_BAND`, `HQ_COUNTRY`,
  `TECH_STACK`, `SITE_PAGE_COUNT`.
- *Competitor* (only if competitors are listed; details in `/gtm-competitors`):
  `COMPETITOR_CUSTOMER` (free: named on a competitor's customers page or loads its script),
  `COMPETITOR_REVIEW_NEGATIVE` (paid: a low competitor review that names the reviewer's company).
Say plainly which cost money (the `cost_class`/`cost_note` columns) and that every run is capped by
the per-run budget in the config (default $3 Apify, 60 Snov credits). An email only goes out when a
fresh intent or trigger exists for that company, so confirm at least one intent or trigger code.

## 3. Save

Write one YAML patch (temp dir) and run `bin/gtm config merge --product <slug> --file <patch>`;
it validates and refuses bad values. Then
`bin/gtm signals confirm --product <slug> --codes CODE1,CODE2,...` for what the founder chose, and
`bin/gtm signals reject --product <slug> --codes ...` for what they turned down.

```yaml
icp:
  segments:
    - {name: "Shopify D2C", industries: [e-commerce, retail], size_band: ["11-50", "51-200"],
       geos: [US], tech_stack_any: [Shopify]}
personas:
  - {name: "Founder/CEO", titles: [CEO, Founder, Co-founder], seniority: c_level}
  - {name: "Head of Growth", titles: [Head of Growth, VP Marketing, CMO], seniority: vp}
anti_icp: [government, education]
signals:
  linkedin: {keywords: ["cart abandonment", "checkout drop-off"]}
  competitors: [Klaviyo]
  hiring_keywords: ["CRO specialist", "Retention marketing manager"]
  pain_language: {queries: ["abandoned cart recovery"], subreddits: [shopify, ecommerce]}
```

`config merge` replaces whole lists (e.g. all personas), so include the existing items you keep.

## 4. Finish

Summarize in plain words: who we target, who gets the email, what we listen for, which sources
are on or off (and why, from the source test), which signals are on and which cost money, and
where the intent sheet is. Next step: `/gtm-clients` if they haven't added clients yet (the
lookalike engine), otherwise `/gtm-accounts` to build the target company list.
