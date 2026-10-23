---
name: gtm-setup
description: First-run and resume wizard for the GTM-Tools cold-outreach automation. It asks the founder, one plain-language question at a time, for everything the automation needs (product name and website, Claude plan, target countries, sender identity, existing clients, budgets, schedule, Snov mailbox) and saves it validated. Use it whenever someone wants to set up, configure, onboard, start, or "get the outreach automation ready", adds a new product, says "what do you need from me", or when another /gtm-* skill reports missing setup, even if they don't say "setup".
---

# GTM setup wizard

The founder is not a developer. He should never open a config file. Your job: find out what is
missing, ask for it in plain language one thing at a time, save it through `bin/gtm`, and finish
with a short "done / still needed" summary. Read `plan.md` §8 if you need the reasoning behind a
default.

## Ground rules (why they matter)

- **Secrets and the PIN never go through chat.** API keys and the approval PIN are what let the
  system spend money and send email as him. Typed into chat, they end up in transcripts. So he
  types them in his own terminal (`bin/gtm secrets set`, `bin/gtm keys init`), and those commands
  refuse to run inside Claude Code anyway. If he pastes a key into chat, tell him plainly that it
  is now exposed and to create a new one in that service, then continue.
- **One question at a time**, using AskUserQuestion with a recommended option first. A wall of
  questions makes non-technical users guess.
- **Re-ask only what's missing.** Start from `bin/gtm setup status`; this skill must work as a
  resume ("finish my setup") as well as a first run.
- **Everything is saved through `bin/gtm`**, never by editing `product.yaml` directly. `config merge`
  validates and refuses unsafe changes (for example, unblocking India without a legal sign-off).
- Plain language. Say "countries you want to email", not "geo_policy".

## Flow

### 1. Where are we?
Run `bin/gtm setup status` (add `--product <slug>` once you know the product). Read the JSON:
`products`, `signing_keys`, `secrets`, `human_actions`, and `product.missing`.

### 2. Product
If there is no product yet, ask for the product's **name** and **website URL**. Make a short
lowercase slug from the name (letters, digits, hyphens; e.g. "Acme CRM" -> `acme-crm`) and run:
`bin/gtm product create --slug <slug> --name "<name>" --url <url>`.
If several products exist and he hasn't said which one, ask. Products never share data, so it
matters which one you're configuring. From here on, every command uses `--product <slug>`.

### 3. Things only he can do (his own terminal)
If `human_actions` lists anything, explain each in one sentence and give the exact command:
- `bin/gtm keys init`: "creates your approval PIN. Every email needs your PIN before it can be
  sent, so nothing goes out without you."
- `bin/gtm secrets set`: "asks for your Apify and Snov.io API keys (hidden as you type). Apify
  finds companies and people on LinkedIn; Snov.io finds and checks email addresses and sends the
  emails." Snov.io API access needs a paid Snov plan; ask him to check if unsure.
Tell him to run them in a normal terminal from the project folder (`cd /mnt/windows/GTM-Tools`),
not with `!` inside Claude Code. Then re-run `setup status` to confirm. Don't block the rest of
the wizard on this; continue and remind him at the end.

### 4. Claude plan
Ask which Claude plan he is on: **Max 5x** or **Max 20x**. It decides how many background AI calls
each run may use (5x: 60 per run; 20x: 150). Unsure means 5x, which is the safe choice.
Save as `usage: {profile: max5x|max20x}`.

### 5. Countries to email
Ask (multiSelect): which countries' companies does he want to email? Options: United States,
India, EU/UK, Other. Explain the result in plain words:
- **US**: allowed. US law needs a real postal address in every email (step 6).
- **India**: India's data-privacy law (DPDP Act) needs a clear notice and an easy way for people
  to have their data deleted. It's blocked until he confirms he has checked this with a lawyer.
  If he says yes, ask who advised and when, then save
  `geo_policy: {IN: {status: allowed_with_notice, legal_signoff: "<who, date>", notice: "<notice>"}}`.
  The default notice: "We found your details on <source>; reply 'stop' and we'll delete them."
  If not, keep India blocked and say he can turn it on later by running this wizard again.
- **EU/UK**: stays blocked (stricter consent rules). Say so; don't argue.
This is information, not legal advice. Say that once.

### 6. Sender identity
Ask for the name emails come from, and a postal address he receives mail at (street address,
PO box or virtual mailbox; US law requires one in cold email). Show the default opt-out line
(`Not interested? Reply "no thanks" and I won't email again.`) and ask whether to keep it.
Save under `voice:`.

### 7. Existing clients (the lookalike engine)
Explain: "your current clients teach the system what a good customer looks like, so it can find
similar companies." Ask him to paste a list of 5–20 clients: company name, website, and if he
knows them, rough deal size and how long they've stayed. Company names and websites are not
secrets, so chat is fine here. Write `products/<slug>/clients.csv` with header
`company,domain,contract_size,retention_months,notes`. Domains are bare (`acme.com`, no `https://`).
Then ask for 10–20 companies that look like a fit but never bought (lost deals, said no). They
keep the scoring honest. Write `products/<slug>/near_miss.csv` with `company,domain,notes`.
Both files stay out of git.

### 8. Budget and schedule
Show the defaults in one message and ask to keep or change them:
- per run: up to $3 of Apify data and 60 Snov credits
- runs at 09:00 (full) and 15:00 (quick refresh), India time, plus an hourly check for replies
  and unsubscribes
Save under `budgets:` and `schedule:` if he changes anything.

### 9. Sending mailbox
Ask whether he has a **separate sending domain** (e.g. `getacme.com`, not his main domain) with a
mailbox connected in Snov.io and warmed up. If not, explain why in two sentences: cold email from
the main domain can put it in spam permanently, and new mailboxes need ~2 weeks of warm-up. Tell
him to set one up now so the warm-up runs while the rest is built. Mailbox registration
(`bin/gtm mailbox add`, his terminal) arrives in Phase 0C; just record his answer.

### 10. Save and summarize
Write the collected answers to one YAML patch file in a temp dir (`mktemp -d`), then
`bin/gtm config merge --product <slug> --file <patch>`. If it's rejected, read the error,
fix the patch, and retry. Never bypass validation.
Finish with `bin/gtm setup status --product <slug>` and a short summary:
- what's done
- what's still needed, and whose action it is (his terminal vs. later phases)
- the next step: `/gtm-onboard` (Phase 1) learns his product from the website once it's built

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
```
