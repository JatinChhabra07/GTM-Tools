---
name: gtm-setup
description: First-run and resume wizard for the GTM-Tools cold-outreach automation. It installs what the automation runs on (knowledge base, last30days engine, embedder), then asks the founder one plain-language question at a time for everything it needs (product, Claude plan, countries, sender identity, budgets, schedule, sending mailbox, Snov campaign) and saves it validated, and finally installs the daily schedule. Use it whenever someone wants to set up, configure, start, or "get the outreach automation ready", adds a new product, asks "what do you need from me" or "what's left", or when another /gtm-* skill reports missing setup, even if they don't say "setup".
---

# GTM setup wizard

The founder is not a developer and never opens a config file. Find out what is missing, ask in
plain language one thing at a time, save through `bin/gtm`, and finish with a short
"done / still needed" list. `plan.md` §8 has the reasoning behind each default.

## Ground rules (why they matter)

- **Secrets and the PIN never go through chat.** They let the system spend money and send email as
  the founder; typed into chat they end up in transcripts. The founder types them in their own
  terminal (`bin/gtm secrets set`, `bin/gtm keys init`), and those commands refuse to run inside
  Claude Code. If a key is pasted into chat anyway, say plainly that it's now exposed and should be
  replaced in that service, then continue.
- **One question at a time** with AskUserQuestion, recommended option first.
- **Re-ask only what's missing.** Start from `bin/gtm setup status`; this must work as a resume
  ("finish my setup") as well as a first run.
- **Save through `bin/gtm`**, never by editing `product.yaml`. `config merge` validates and refuses
  unsafe changes (e.g. emailing India without a legal sign-off).
- Plain language: "countries you want to email", not "geo_policy".

## Flow

### 1. Where are we?
`bin/gtm setup status` (add `--product <slug>` once known). Read `products`, `signing_keys`,
`secrets`, `human_actions`, and `product.missing` / `product.progress`. Skip every step below that
is already done.

### 2. Install the tools (automatic, no questions)
Run `bin/gtm tools install`. It installs the pinned last30days engine (Reddit/Hacker News
listening), starts the local Qdrant database in Docker (the mem0 knowledge base), and downloads the
embedding model. It takes a few minutes the first time and is safe to re-run. Then
`bin/gtm doctor`; if something is red, use `/gtm-doctor`'s fixes before continuing (Docker
missing is the usual one).

### 3. Product
No product yet: ask for its **name** and **website URL**, make a lowercase slug ("Acme CRM" →
`acme-crm`), and run `bin/gtm product create --slug <slug> --name "<name>" --url <url>`.
Several products and none named: ask which. Products never share data. From here on, every command
uses `--product <slug>`.

### 4. Things only the founder can do (their own terminal)
For each `human_actions` item, one sentence and the exact command:
- `bin/gtm keys init`: "creates your approval PIN. Every email needs your PIN before it can be sent,
  so nothing goes out without you."
- `bin/gtm secrets set`: "asks for your Apify and Snov.io API keys (hidden as you type). Apify
  finds companies and people on LinkedIn; Snov.io finds and checks email addresses and sends. A
  Firecrawl key is optional (reads websites wigolo can't)." Snov API access needs a paid Snov plan.
They run these in a normal terminal from the project folder (`cd /mnt/windows/GTM-Tools`), not
with `!` inside Claude Code. Don't block on it; continue and remind at the end.
Once keys are set, `bin/gtm tools check --paid` confirms both keys work (free reads).

### 5. Claude plan
Max 5x or Max 20x? It sets how many background AI calls a run may use (5x: 60; 20x: 150, plus an
extra writing-polish pass). Unsure → 5x, the safe choice. Save `usage: {profile: max5x|max20x}`.

### 6. Countries to email
AskUserQuestion multiSelect: United States, India, EU/UK, Other. Explain:
- **US**: allowed. US law needs a real postal address in every email (step 7).
- **India**: India's data-protection law (DPDP Act) expects a clear notice and an easy way to have
  data deleted. Blocked until the founder confirms they checked this with a lawyer. If yes, ask who
  advised and when, then save
  `geo_policy: {IN: {status: allowed_with_notice, legal_signoff: "<who, date>", notice: "<notice>"}}`
  (default notice: "We found your details on <source>; reply 'stop' and we'll delete them.").
  If not, India stays blocked; they can turn it on later by re-running this wizard.
- **EU/UK**: stays blocked (stricter consent rules). Say so; don't argue.
This is information, not legal advice; say that once.

### 7. Sender identity
The name emails come from, and a postal address they receive mail at (street, PO box or virtual
mailbox). Show the default opt-out line (`Not interested? Reply "no thanks" and I won't email
again.`) and ask whether to keep it. Save under `voice:`.

### 8. Budget and schedule
Show the defaults in one message; keep or change:
- per run: up to $3 of Apify data and 60 Snov credits
- runs at 09:00 (full) and 15:00 (light refresh) in the founder's time zone, an hourly check for
  replies/unsubscribes, and a daily 08:30 reply-drafting pass
Save `budgets:` and `schedule:` (with `timezone`, e.g. `Asia/Kolkata`) if anything changes.

### 9. Learn the product, the targets and the clients
If `missing` lists them, run these skills in order (each asks its own questions):
`/gtm-onboard` → `/gtm-icp` → `/gtm-clients` → `/gtm-accounts` (and `/gtm-competitors` if the
founder named competitors; optional).

### 10. Sending mailbox (the founder's terminal + Snov UI)
Ask whether they have a **separate sending domain** (e.g. `getacme.com`, not the main domain) with
a mailbox connected in Snov.io. If not, explain in two sentences: cold email from the main domain
can land it in spam for good, and a new mailbox needs about two weeks of warm-up, so start now.
When they have one:
1. They register it in their terminal (limits are signed with their PIN):
   `bin/gtm mailbox add --id mb_1 --address <address> --warmup-start <YYYY-MM-DD>`
2. You check the domain: `bin/gtm warmup <sending-domain>`. It checks MX, SPF, DKIM, DMARC,
   domain age and blocklists, and marks the mailbox READY or NOT READY with reasons. Explain any
   failures in plain words (the fix is usually one DNS record at the domain registrar).
3. Save which mailbox this product uses: `sending: {mailboxes: [{id: mb_1}]}` via `config merge`.
4. `bin/gtm snov setup --product <slug> --mailbox mb_1` creates the Snov list and the
   do-not-email list and prints a 5-minute checklist for the Snov UI (custom fields, a 3-step
   campaign, tracking off, stop-on-reply on). Walk the founder through it step by step.
5. With the campaign id from the Snov URL:
   `bin/gtm snov link --product <slug> --mailbox mb_1 --list-id <id> --campaign-id <id>`. It
   verifies the campaign settings; if it reports a problem, tell them which toggle to fix and re-run.

### 11. Save, schedule, summarize
Write collected answers to one YAML patch in a temp dir (`mktemp -d`) and run
`bin/gtm config merge --product <slug> --file <patch>`. If rejected, read the error, fix the patch,
retry. Never bypass validation.
When the ICP, signals and accounts exist, offer to start the daily schedule:
`bin/gtm schedule install --product <slug>` (user crontab; `schedule show` / `schedule remove`).
Scheduled runs only *prepare* emails; nothing is sent until the founder approves in
`bin/gtm review`.
Finish with `bin/gtm setup status --product <slug>` and a short summary: done, still needed (and
whose action: their terminal, the Snov UI, or a skill), and the next step (usually `/gtm-run` for
a first test run).

## Example patch

```yaml
usage: {profile: max5x}
voice:
  sender_name: "Ravi Kumar"
  postal_address: "221B MG Road, Bengaluru 560001, India"
geo_policy:
  US: {status: allowed}
  IN: {status: blocked}
  EU: {status: blocked}
  UK: {status: blocked}
  default: {status: blocked}
schedule: {timezone: Asia/Kolkata}
sending: {mailboxes: [{id: mb_1}]}
```
