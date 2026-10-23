# GTM-Tools — Signal-Based Cold Outreach Automation

> **Goal:** paste a product website, then get **up to 10–15 high-quality, verified leads per day**, capped by safe sending capacity (≈ 10/day per warmed mailbox). Each lead is backed by a real buying signal or trigger and gets a personalised email sequence. Runs on a schedule on your Claude Code subscription, with one knowledge base per product.
>
> **Principle:** small tools ("micro GTM tools") that each do one job, share one validated data contract, and can run alone or chained. Quality over volume at every step.

Status: **PLAN v3.3** · 2026-10-23 · **Phase 0 in progress** · Owner: Jatin · Workspace: `/mnt/windows/GTM-Tools`

How we got here:
- **v1:** graph-engineering review (4 lenses + verification): 45 findings, 2 blockers.
- **v2 / v2.1:** two gate checks.
- **v3:** fixes the last gate's findings and adds the **verified** best parts of [Harvey](https://github.com/ethanplusai/harvey) (MIT, commit `f851026`, read from source by 3 independent reviewers).

See **§11**. Older versions are in `docs/`.

---

## 0. Core design decisions

| # | Decision | Why |
|---|---|---|
| 1 | **Two surfaces.** *Interactive*: Claude Code skills (`/gtm-*`) for setup, ICP, review, reports. *Scheduled*: a plain Python process `gtm run` (cron), which calls Claude through **isolated, tool-less `claude -p`** for judgement. | The scheduled pipeline needs no MCP OAuth, no permission allowlists and no Workflow-in-headless. It runs on your Claude subscription (Harvey's proven pattern). |
| 2 | **Quarantined LLM.** Any model call that reads untrusted text (scraped posts, sites, replies) runs in `gtm/llm.py` with no tools, no MCP, no skills, no settings, an empty temp directory and no session saved. | A prompt injection in a scraped post has nothing to act with. |
| 3 | **Code does every paid, irreversible or stateful action.** Apify, Snov, last30days, SQLite, mem0, budgets, gates and sending are `gtm` code paths. | Budgets, resume and "never send unapproved" are enforced in code, not in prompts. |
| 4 | **Humans approve; code enforces, cryptographically.** `gtm review` runs in **your own separate terminal**, never inside Claude Code. Each approval is **signed with an Ed25519 key that is encrypted under your PIN** (scrypt; key file in `~/.config/gtm-tools/`, which is denied to Claude). The signature covers the *fully rendered* emails (subject, body, footer, sender, postal address, notice) + lead + recipient + mailbox. `gtm run` verifies every signature with the public key before pushing. The same applies to enabling auto-send and resuming sending. | Rows written to SQLite by anything else (an over-eager session, a script) can't carry a valid signature, so they are never sent. |
| 5 | **Every fact is a row** (`observations`), with a governed vocabulary, confidence and an evidence URL. Signals you haven't **confirmed** for a product are never collected. | Explainable lists, history for free, no schema migration per new signal (from Harvey, with its cohort bugs fixed). |
| 6 | **Two lead sources.** Account-first (target account list + re-observed triggers) and signal-first (named people expressing need). Anonymous posts are research input, never leads. | Anonymous handles can't be resolved to a person; companies that never post would otherwise be invisible. |
| 7 | **Knowledge and leads are isolated per product; compliance data is global.** | An unsubscribe is per person, not per product. |
| 8 | **Filter cheap before you pay; estimate before you spend.** | Credits are spent only on accounts that can qualify. |
| 9 | **Quality = outcomes** (positive replies, meetings per 100 delivered) with sample thresholds and a holdout. | Opinions aren't metrics. |
| 10 | **Fixtures first.** Real API responses are recorded in Phase 0; `GTM_FAKE=1` replays them. | Building against live paid APIs burns budget on debugging. |

---

## 1. Architecture

### 1.1 Flow

```
 ── INTERACTIVE (Claude Code skills, you in the loop) ─────────────────────────────────
  /gtm-onboard --url     crawl product site (wigolo MCP) → knowledge/*.md → facts → mem0
  /gtm-icp               propose ICP, personas, anti-ICP, geo policy, SIGNAL CATALOG
                         → you confirm/reject each signal (nothing unconfirmed is collected)
  /gtm-clients           clients.csv → profile + enrich → fingerprint → calibrated pre-score
  /gtm-accounts          build Target Account List (TAL) with a cost estimate first
  gtm review            in YOUR OWN terminal (not inside Claude Code): one sequence at a
                        time, evidence beside it; TTY + PIN; signs approvals
  /gtm-report /gtm-doctor /gtm-run (manual trigger of the same Python run)

 ── SCHEDULED: cron → bin/gtm-scheduled-run → `uv run gtm run --product P --mode M` ────
  one process, holds the product lock for the whole run, fixed order:
   1. replies & suppression sync (Snov) ── if this fails: NO sends this run
   2. drain outbox: approved + hash-matching + re-gated sequences → Snov (atomic claim)
   3. collect   triggers (TAL re-observation, jobs, news, site diffs)  + signals (social)
   4. resolve   explicit evidence only → accounts; pre-score; global ledger check
   5. enrich    personas (Apify) → email pattern-first → Snov verify   [within daily cap]
   6. qualify   graph in Python: worker → 4 lens reviewers → synth → gate (isolated calls)
   7. write     FACTS-only prompt → humanize → code validator → outbox (pending_review)
   8. report    funnel, cost, usage, failures
  daily: gtm feedback (reply classification, analytics) · weekly: proposals + TAL refresh
```

### 1.2 Execution boundary (must be followed exactly)

| Layer | Does | Never does |
|---|---|---|
| **`gtm` Python** | Apify (`apify-client`, `max_items` + charge cap), Snov REST (find, verify, push, replies, bounces, unsubscribes), last30days engine subprocess, wigolo through an MCP **stdio client** (`mcp` Python SDK → `npx -y wigolo`), the public-HTTP site profiler, SQLite, mem0, gates, validator, budgets, outbox | Make judgement calls without `gtm/llm.py` |
| **`gtm/llm.py` (isolated `claude -p`)** | Intent labelling, resolution from evidence text, scoring reviews, email writing, humanizing, reply classification and drafting | Have tools; see unescaped untrusted text outside `<untrusted>` blocks |
| **Claude Code skills (interactive session)** | Talk to you, propose ICP and signal catalog, read *your own* product site (wigolo MCP), call `gtm` commands | Call Apify/Snov MCP write tools, approve, send, edit SQLite |
| **Human** | Confirm signals, approve ICP, approve sequences, approve weight changes, legal sign-off | — |

LLM I/O shape inside `gtm`: build input JSON → `llm.call(prompt_id, input, schema)` → validate against the pydantic schema → on failure retry up to 2× (an outage doesn't count as an attempt), then mark `needs_human`.

### 1.3 Isolated LLM runtime (`gtm/llm.py`, adapted from Harvey `claude_runtime.py`)
- Command: `claude -p --output-format json --tools "" --strict-mcp-config --safe-mode --setting-sources "" --disable-slash-commands --no-session-persistence --model <id>`. The prompt goes on **stdin**, cwd is a fresh empty temp dir, and the env is stripped of overrides. Stripping `ANTHROPIC_API_KEY` is our addition, so calls stay on your subscription.
- **Help-probe before use:** if `claude --help` lacks any required flag, refuse with "run `claude update`". Never fall back to an unisolated call. All seven flags are present in the installed CLI 2.1.309 (checked 2026-10-23).
- **Leak test (Phase 0 exit):** plant a codeword in a CLAUDE.md, a skill and a hook; an isolated call must not reveal it.
- **Usage ledger:** tokens per call from the JSON `modelUsage` go into `usage_events(run_id, prompt_id, model, in, out)`. Cap `budgets.llm_calls_per_run`. Quota readings are best-effort only (Harvey uses an undocumented endpoint; we don't depend on it).
- Uses whatever auth the `claude` CLI has (your subscription). Check your plan's usage limits and terms for automated use.

### 1.4 Tool mapping (verified 2026-10-23; re-verified in Phase 0)

| Job | Tool | Notes |
|---|---|---|
| Crawl your product site (interactive) | wigolo MCP in-session | Free, local |
| Web/news search, page diffs (scheduled) | wigolo via MCP stdio client from Python | Verify in Phase 0. Fallback: Apify `apify/google-search-scraper` |
| Company website profiling | **`gtm/profile.py`** over **`gtm/public_http.py`** (both adapted from Harvey) | Free. Homepage, robots, sitemap, team/careers/contact pages; tech, ad pixels, hiring roles, named people |
| Knowledge base | **mem0 OSS** `mem0ai==2.2.1` + **Qdrant server** (docker) + local HF embedder | `infer=False` everywhere (§2.2), so no extra LLM key |
| Reddit / HN | last30days engine at a pinned git tag: `scripts/last30days.py '<q>' --emit=json --search reddit,hackernews --save-dir ''` | → `pain_language` only, never leads |
| Hiring | profiler careers pages + `curious_coder/linkedin-jobs-scraper` (Apify) | |
| LinkedIn posts, companies, people | Apify `harvestapi/linkedin-post-search`, `linkedin-company`, `linkedin-company-employees`, `linkedin-profile-search` | Pay-per-event; always `max_items` + charge cap. No personal cookies |
| X | Apify `apidojo/tweet-scraper` | Named accounts only |
| Instagram | Apify `apify/instagram-scraper` | Off by default |
| Email find/verify | Pattern-first (`email_patterns`) → **Snov REST** verify (async: start → poll) | 60 req/min. `smtp_status` valid / not_valid / unknown (+ reason) |
| Sending | **Snov REST**: push an approved prospect with custom fields to the product's list → campaign built in the Snov UI | "Stop after a company reply" ON; **open/click tracking OFF**; send window = recipient business hours |
| Snov / Apify MCP | Interactive exploration and read-only use | Write/run tools denied in `.claude/settings.json` |
| Optional per-recipient legal check | [Precleared](https://precleared.dev) | Off by default. It receives the recipient address (third-party sharing) and may be related to Harvey's author. Hold on error, block on "no" |

### 1.5 Existing assets
- `~/.claude/agents/*` outreach agents are Saksham-tuned → templates only.
- Harvey → adopted *ideas and adapted code* with attribution in `NOTICE` (MIT). We never vendor its runtime wholesale.

### 1.6 Folder layout

```
/mnt/windows/GTM-Tools/                  ← git repo: code, skills, playbooks, configs. No secrets/PII.
├── plan.md  CLAUDE.md  NOTICE  pyproject.toml  .env.example
├── bin/gtm-scheduled-run
├── .claude/settings.json                ← deny list for interactive sessions (§6.2)
├── .claude/skills/gtm-{onboard,icp,clients,accounts,run,report,doctor}/SKILL.md
├── playbooks/                           ← editable sales knowledge (Markdown, §5)
│   writing.md signals.md replies.md accounts.md compliance.md
├── prompts/                             ← isolated-call prompts, one per prompt_id
├── gtm/
│   cli.py schemas.py store.py ledger.py observations.py kb.py llm.py budget.py
│   public_http.py profile.py email_finder.py score.py gates.py validate.py
│   outbox.py qualify.py writer.py handler.py warmup.py run.py fake.py
│   sources/{apify,snov,last30days,wigolo}.py
├── products/<slug>/ product.yaml  facts.yaml  learnings.jsonl  knowledge/*.md
├── tests/ fixtures/<tool>/*.json  fixtures/run_seed/  fixtures/calibration.yaml  test_*.py
└── docs/ tools.md compliance.md plan-v*.md plan-review-*.txt

~/.config/gtm-tools/env                  ← secrets, chmod 600 (ext4; NTFS shows every file as 777)
~/.local/share/gtm-tools/                ← GTM_DATA_DIR (hard-coded)
├── qdrant/                              ← Qdrant server volume
├── _global/contacts.db                  ← global ledger (§2.3)
└── <slug>/ state.db  mem0/history.db  .mem0/  lock  runs/<run_id>/
```

Runtime data stays off `/mnt/windows`, because SQLite WAL and file locks are unreliable on NTFS-over-FUSE.

---

## 2. Storage & isolation

### 2.1 Rules
1. `--product <slug>` is required on every product command. Exempt global commands: `gtm product create|list`, `gtm doctor`, `gtm warmup`, `gtm sending pause|resume`, and `gtm forget <email>` (one subprocess per product).
2. No cross-product knowledge or lead queries: one `state.db` per product; Qdrant collection `gtm_<slug>`; `filters={'user_id': slug}`; per-product `history_db_path` and `MEM0_DIR`. **The exception is the global ledger (§2.3), by design.**
3. **Product lock:** `gtm run` holds `flock(<slug>/lock)` for its *whole* duration. Every other mutating command takes the same lock. A second run of the same product exits immediately with "already running".
4. Skills always start with `gtm product show` so the session states which product it is on.

### 2.2 mem0 (`gtm/kb.py`)
- Qdrant **server** (`docker run -d --restart unless-stopped -p 6333:6333 -v ~/.local/share/gtm-tools/qdrant:/qdrant/storage qdrant/qdrant`), `embedding_model_dims` matching the local embedder (e.g. 384). Embedded local Qdrant is not allowed (it takes an exclusive per-process lock).
- **`infer=False` on every add.** Extraction is done beforehand, by the interactive skill for onboarding or by `gtm/llm.py` in runs. So mem0 never rewrites or deletes memories itself, and **no separate LLM API key is needed**.
- `MEM0_TELEMETRY=False`, `MEM0_DIR` and `history_db_path` set per product. Calls use `search(q, filters={...})` / `get_all(filters={...})`.
- `kb.py` is the only writer. Before adding, it dedupes by `sha1(category + normalized text)`. It **refuses update/delete of `metadata.authority='human'`** except through `gtm kb reload-facts`, which reloads `facts.yaml`/`product.yaml`.
- **No personal data:** `kb.add` rejects email/phone patterns. `learning` memories are aggregates only. Lead-derived memories carry `metadata.lead_key` so erasure can find them.
- Categories: `product_fact`, `proof` (+ `source_url`), `icp`, `client_pattern`, `competitor` (**only competitors named on the site or by you, never inferred**), `pain_language`, `learning`, `voice`.

### 2.3 Global ledger (`_global/contacts.db`; written only by `gtm`; all writes in `BEGIN IMMEDIATE`)
- `suppression(email_hash, domain, linkedin_hash, reason, scope, added_at)`: unsubscribe, bounce, complaint, erasure, manual.
- `sequences(id, product, lead_key, email_hash, domain, mailbox_id, status, step_sent, started_at, ended_at)`, where status ∈ `active | finished | replied | opted_out | bounced | stopped`.
- `contact_log(email_hash, domain, product, mailbox_id, step, email_class, sent_at)`.
- `mailbox_sends(mailbox_id, date, touches)`: the global per-mailbox counter in **touches** (emails), not sequences. Pushing a sequence reserves one touch on each scheduled day (day 0, +4, +12 per `followup_days`). The navigation check, the `sequences` insert and all touch reservations happen in **one `BEGIN IMMEDIATE` transaction in `contacts.db`**. A push is refused if any day would exceed the mailbox's effective cap, whichever product is pushing. If the later Snov push fails *before* sending, a compensating transaction releases the reservation.
- **One Snov campaign per (product, mailbox)**, so the Snov sender really is `mailbox_id`. Verified in Phase 0.
- `mailboxes(id, address, daily_cap, warmup_start, warmup_min_days, start_sends, daily_increase, ready_state, bounce_rate_7d, paused, catchall_allowed)`.
- `sending_state(paused, reason, at, resume_signature)`: the global kill switch. Pausing is allowed from anywhere; **resuming needs a PIN signature**.
- `autosend(product, enabled, enabled_at, signature)`: valid only with a PIN signature from `gtm autosend enable`. Disabling needs no PIN.
- Signing keys: `~/.config/gtm-tools/approval_key.enc` (private, scrypt-encrypted with your PIN) and `approval_key.pub`. `gtm keys init` (TTY) creates them; changing the PIN requires the old PIN.
- `email_patterns(domain, pattern, confidence, source, learned_at, expires_at)`, `clients(domain, name, contract_size, retention)`, `watermarks(product, source, last_collected_at)`: in each product's `state.db`.
- `erasure_log(email_hash, at, scopes_cleared)`.

---

## 3. Data contract (`gtm/schemas.py` + SQLite)

```text
Run          run_id ("YYYYMMDD-HHMMSS-<4hex>"), product, mode ∈ {full, refresh, sync, noop}, started, finished,
             status ∈ {running, done, partial, failed}, attempt (≤ 2), counts, cost, failure_reason
Step         run_id, step, status, external_ref (Apify run id / Snov task_hash, saved BEFORE waiting)

SignalCode   code (PK), product, status ∈ {proposed, confirmed, rejected}, cost_class ∈ {free, paid},
             cost_note, kind ∈ {intent, trigger, firmographic, contact, health}, value_type ∈ {bool, num, text},
             system (bool: `contact` and `health` codes are always confirmed and can't be rejected),
             threshold (for num: op + value), min_confidence, freshness_days, rank (for the writer)
             — DB trigger: observations.signal_code must exist and be `confirmed`
Observation  id, account_domain, lead_key?, collector, signal_code, value_num?, value_text?,
             confidence (0–1), evidence_url (required), observed_at, run_id   (append-only)
Post         id = sha1(source+url), source, url, posted_at, author_name?, author_handle?, author_title?,
             author_employer_evidence?, text_excerpt (≤ 600 chars, control/zero-width stripped),
             resolvability ∈ {named_person, company_event, anonymous}, matched_keywords[], run_id

Account      domain (PK), name, industry, size_band, hq_country, linkedin_url, source ∈ {tal, signal},
             tal_tier?, resolution_evidence (URL, required), lookalike_score, prescore (0–100),
             profile_checked_at, status
Lead         lead_key = sha1(product + normalized linkedin_url) or sha1(product + lower(name) + domain)
             (UNIQUE), account_domain, full_name, title, persona, persona_match ∈ {exact, fuzzy, llm},
             seniority, linkedin_url, email (lower, UNIQUE per product), email_source ∈ {pattern,
             snov_finder, site_printed, generic_inbox}, smtp_status, unknown_status_reason?,
             email_class ∈ {valid, catchall, invalid, unknown}, is_generic_inbox, is_signal_author,
             referral (bool), source_observation_ids[], cost_usd
Score        lead_key, total (0–100), dims{icp_fit, intent, lookalike, authority, contactability},
             gates{…§4.1}, reviewer_findings[], verdict ∈ {keep, drop, needs_rework}, drop_reason?,
             holdout (bool: sampled BEFORE the graph, §4.3), feature_snapshot (frozen JSON)
Sequence     seq_id, lead_key, steps[{step 1–3, subject, body}], facts_used[observation_id],
             reference_policy ∈ {self_public_professional, company_event, theme_only}, proof_ids[],
             recipient_email, mailbox_id,
             content_hash = sha256(seq_id + lead_key + recipient_email + mailbox_id + every step
                            FULLY RENDERED: subject, body, footer, sender name, postal address, notice),
             status ∈ {draft, pending_review, approved, rejected,
             claimed, pushed, failed_maybe_sent, cancelled}, writer_attempts, feedback_notes[]
Approval     target ∈ {sequence, reply_draft}, target_id, content_hash, approved_by (OS user | "policy:<rule>"),
             approved_at, via ∈ {tty_pin, policy}, edited (bool), signature (Ed25519 over all fields)
             — `tty_pin` approvals are signed in `gtm review`. `policy` approvals are valid only while a
               signed `autosend` record exists and the push-time gates pass; holdout leads are never
               policy-approved.
Reply        id (provider id, UNIQUE), seq_id, received_at, body_stripped, intent, keyword_override?,
             status ∈ {new, handled, needs_human}
ReplyDraft   id, reply_id, recipient_email, mailbox_id, body,
             content_hash = sha256(id + reply_id + recipient_email + mailbox_id + rendered body),
             status ∈ {pending_review, approved, rejected, claimed, sent_api, sent_manual, failed_maybe_sent}
             — API sends go through the same outbox ladder (signature check, atomic claim, re-gate,
               never auto-retry)
```

Rules:
- Re-writing a sequence creates a new `content_hash` and resets status to `pending_review`; the old approval no longer matches.
- Pushing requires a **valid signature** and `Approval.content_hash ==` the hash *recomputed at push time* from the current rendered emails. So a changed footer, address or `product.yaml` voice field after approval → no push.
- Edits are allowed only in `pending_review`.
- There is no blanket approve-all (§5 Phase 7).

---

## 4. Lead quality model

### 4.1 Hard gates (`gates.py`, deterministic; any failure = drop; the LLM can't override)
- `email_class == valid`. A `catchall` address is allowed only if `allow_catchall`, score ≥ 85, ≤ 20% of sends, and the mailbox has `catchall_allowed`. A generic inbox (`info@`, `hello@`, `office@`…) is allowed only if it is printed on the company's own site, no named person was found, and it is verified. Its emails greet the team, not a name.
- Not a client, not a competitor, not suppressed (email, domain or LinkedIn hash).
- **Account navigation** (global ledger, checked and recorded atomically at claim time, see `mailbox_sends`), all products:
  - no other product has an `active` sequence or contact within `recontact_days` (90)
  - within this product: **one active contact per account at a time**, the next contact only ≥ 5 business days after the previous sequence `finished` with no reply, at most 3 silent contacts per account per 90 days
  - any `replied`, `opted_out` or `bounced` sequence closes the account for 90 days
  - `referral=true` leads (a person named in a reply, e.g. "talk to X") **override the 90-day closure** caused by that reply, for the named person only
- Freshness by `SignalCode.freshness_days` (re-checked at push time): `ASKS_FOR_SOLUTION` 14 · `COMPETITOR_PAIN`/`SWITCHING`/`EXEC_POST_TOPIC` 21 · `HIRING_ROLE`/`HIRING_FOR_PROBLEM` 45 or while the post is live · `FUNDING` 60 · `LEADERSHIP_CHANGE` 90 · `LAUNCH` 45 · `EXPANSION` 60 · `SITE_CHANGE`/`PRICING_CHANGE`/`TECH_ADDED`/`TECH_REMOVED`/`RUNNING_*_ADS` 30.
- Persona match: exact/fuzzy title match first, then a cached isolated-LLM fallback. ICP titles are literal business-card titles.
- `geo_allowed`: location (lead profile, else company HQ) decides. A country-code TLD can only add a block; generic TLDs are neutral. Blocked only when no location is known.

### 4.2 Score (`score.py`, 0–100; isolated reviewers can only lower or drop)

| Dimension | Default | Notes |
|---|---|---|
| ICP fit | 30 | firmographic observations vs the approved ICP |
| Intent | 30 | intent observations: anchored strength 3/2/1 × recency decay. **Triggers can supply at most 40%** of this dimension. Stacking is capped. |
| Lookalike | auto | n < 10 clients: 5 · n 10–30: 10 · n > 30: 20 (the rest moves to intent). Clients weighted by contract size/retention. |
| Authority | 10 | `seniority` (required per persona) |
| Contactability | 10 | `email_class`, signal author, recent activity |

- **Pre-score** (accounts, before paying) = ICP fit + lookalike, normalised to 0–100. Cut: `scoring.prescore_cutoff` (default 60).
- **Final** cut: `scoring.cutoff` (default 70).
- **Daily capacity:** `cap = Σ (mailbox effective_daily_cap × product share) / touches_per_lead`. Effective cap = the warmup ramp value (§6.3). `sending.mailboxes: [{id, share}]`, and `gtm doctor` fails if one mailbox's shares across products add up to more than 1.0. The global `mailbox_sends` counter is the hard limit at push time.
- **Before sending exists** (Phases 3–6, or no READY mailbox yet): `cap` comes from `--limit N` (default 10) on `gtm enrich`/`gtm run`. That is enough for live checks and the funnel go/no-go.
- **Enrichment N per run** = `cap − leads enriched today − max(0, pending_review backlog − cap)`. If the backlog is ≥ 2 × cap, skip prospecting this run (from Harvey's "idle when unusable backlog").

### 4.3 Qualification graph (`gtm/qualify.py`, graph-engineering pattern in deterministic Python)
- Rubric = §4.1–4.2, written to `runs/<id>/rubric.json` **before** scoring.
- **Worker** (1 isolated call): per-lead scores from FACTS (observations + deterministic dims).
- **4 reviewers** (parallel isolated calls, distinct lenses, prompted to refute):
  1. ICP & lookalike
  2. Signal truth & resolution evidence
  3. Person fit & email ownership
  4. Risk: competitor, client, student, reseller, compliance, creepy reference
- **Synth** (1 call): keep / drop (+ reason) / needs_rework.
- **Round 2** (≤ 1 call) for `needs_rework` only.
- Total ≤ 7 isolated calls per run. Non-converged leads are dropped with reason "unconverged", never passed.
- **Holdout:** before the graph runs, 20% of gate-passing leads are sampled as `holdout`. They skip the reviewers and are kept if their deterministic score ≥ cutoff. Comparing reply rates of holdout vs graph-kept leads measures what the graph adds.

### 4.4 Funnel model (measured in Phases 3–5; targets are per day)
Collected observations → resolvable % → pre-qualified accounts → persona found % → emailable % (catch-all share) → qualified % → approved %.

**Go/no-go before Phase 6:** projected qualified/day = pre-qualified accounts/day × persona% × emailable% × 0.5. If < 5/day, widen the ICP/TAL or add sources first.

### 4.5 Success metrics
- North star: positive replies and meetings per 100 delivered.
- Guardrails: bounce < 2%, opt-out rate, % sequences you rejected.
- `feature_snapshot` is logged per send.
- Weight/ICP proposals and `learning` memories need ≥ 100 sends and ≥ 5 positive replies in the segment, shown with confidence intervals. You approve each change.

---

## 5. Phases

Each phase passes on fixtures first (`GTM_FAKE=1`), then **one** small live check. Start each session with:

> "Read `plan.md` and `CLAUDE.md`. We are doing **Phase N**. Run the tests, then build Phase N."

Grammar: `bin/gtm <cmd> --product <slug> [--run <id>]`. `bin/gtm` wraps `uv run gtm` and keeps the Python environment on the Linux disk (`~/.local/share/gtm-tools/venv`), because NTFS-over-FUSE is too slow for torch-sized packages. Skills take `--product <slug>`.

---

### Phase 0: Foundations, safety rails & tool truth *(3 sessions: 0A + 0B in parallel, then 0C)*

| Part | Session | Builds | Needs |
|---|---|---|---|
| **0A** | Session 1 | repo, `bin/gtm`, schemas (`state.db` + vocabulary trigger, `contacts.db`), `gtm/llm.py` isolated runtime + help-probe + leak test, Ed25519 signing (`gtm keys init`), CLI basics, `/gtm-setup` wizard, `.claude/settings.json` deny list | nothing |
| **0B** | Session 2 (parallel, own git worktree) | `gtm/public_http.py` (SSRF-safe client), `gtm/validate.py` (email validator rules §5 Phase 7), `playbooks/*.md` + the playbook lint test | 0A skeleton committed |
| **0C** | after the founder runs `/gtm-setup` | Qdrant + mem0 round-trip, Apify/Snov/wigolo/last30days tool truth + fixtures, `gtm warmup`, `gtm mailbox add`, cron `noop` run | API keys + mailbox from the founder |


**Build**
- Repo basics:
  - `git init`, `.gitignore`
  - `CLAUDE.md`: §9 rules + execution boundary
  - `NOTICE`: Harvey MIT attribution
  - `uv` project with pinned deps: `mem0ai==2.2.1`, `sentence-transformers` + CPU torch, `apify-client`, `mcp`, `httpx`, `pydantic`, `typer`, `cryptography` (Ed25519 + scrypt)
- Secrets in `~/.config/gtm-tools/env` (600): `APIFY_API_TOKEN`, `SNOV_CLIENT_ID`, `SNOV_CLIENT_SECRET`, optional `PRECLEARED_API_KEY`.
- Schemas:
  - `state.db`: all §3 tables; `observations` + `signal_codes` with the **vocabulary trigger**; UNIQUE keys; migrations via `PRAGMA user_version` (append-only list)
  - `contacts.db`: §2.3
- **`gtm/llm.py`**: §1.3, help-probe, usage ledger, **leak test**.
- **`gtm/public_http.py`** (adapted from Harvey):
  - every DNS answer and redirect hop must resolve to a public IP
  - connect to the checked IP while keeping the Host header and TLS SNI
  - ignore proxy env vars
  - **plus a response-size cap and timeouts**, which Harvey lacks
- Qdrant container; `kb.py` round-trip.
- **Tool truth** (tiny inputs) → `tests/fixtures/` (PII stripped) + `docs/tools.md`:
  - Apify actors at `max_items: 5`: fields, real charged cost, date filters, charge cap
  - Snov: API access on your plan; async verify; `smtp_status` for 10 known emails (catch-all share); custom field count and length per prospect; can campaign steps be pure `{{field}}` templates (decides personalised follow-ups); "stop after a company reply"; tracking can be turned off; how bounces/unsubscribes/replies come back over the API; **can the API stop a prospect in all campaigns, delete a prospect, and pause/resume a whole campaign** (needed for opt-out and erasure; if not, the run report lists manual Snov actions daily); **can the API send an in-thread reply** (else approved reply drafts are sent by you from the mailbox); exact MCP tool names for the deny list
  - wigolo via the Python MCP stdio client: one `search`, one `fetch`, one `diff`
  - last30days JSON `schema_version`
- `gtm warmup <domain>` (adapted from Harvey `deliverability.py`):
  - checks MX, SPF (`+all`, 10-lookup limit), DKIM selector, DMARC policy, domain age (RDAP), blocklists
  - a failed lookup counts as unknown, not missing
  - prints READY / NOT READY and **stores it on the mailbox**
- `bin/gtm-scheduled-run`:
  - `mountpoint -q /mnt/windows`; source nvm; set PATH; absolute paths
  - **no secrets exported** (`gtm` reads the env file itself); `unset ANTHROPIC_API_KEY`
  - `timeout 50m uv run gtm run --product P --mode M`; log to the run dir; notify on non-zero exit
- Minimal `sources/apify.py` and `sources/snov.py` clients (used from Phase 2 on).
- `gtm keys init` (TTY): PIN + Ed25519 signing keys (§0 #4). `gtm mailbox add` (TTY + PIN): address, daily_cap, warmup fields, catch-all flag.
- **Start mailbox warmup now** (Snov warm-up or your warmup tool) and register the mailbox with `warmup_start`, so the 14-day clock runs during Phases 1–6.
- `.claude/settings.json` deny list (§6.2). `docs/compliance.md` draft.

**Exit criteria**
- [ ] `pytest` green: vocabulary trigger rejects an unknown or unconfirmed code; SSRF tests (private IP, redirect to 169.254.x, DNS rebinding)
- [ ] Leak test passes; help-probe refuses when a flag is faked as missing
- [ ] A cron entry runs `gtm run --mode noop`: 1 isolated call + 1 wigolo call + 1 SQLite write, with no prompts. It also succeeds while you are logged out of the desktop.
- [ ] `docs/tools.md` + fixtures complete; Snov checklist fully answered
- [ ] `gtm warmup` on your sending domain prints a verdict; no secrets in git; env file mode 600

---

### Phase 1: Product knowledge base *(1–2 sessions)* → `/gtm-onboard --product <slug> --url <url>`

**Build**
- The skill crawls with wigolo (sitemap → bfs, ~60 pages; product, pricing, customers, case studies, about) → `knowledge/*.md`.
- The session extracts facts as JSON: `product_fact`, `proof` with `source_url`, `competitor` (**only named on the site**), `voice`. Then `gtm ingest facts` → mem0 (`infer=False`).
- Optional product MCP as an extra source.
- **Product Brief** for your review → corrections go to `facts.yaml` → `gtm kb reload-facts` (`authority=human`).
- Re-onboarding uses wigolo `diff`: changed pages only; human facts untouched.

**Exit criteria**
- [ ] You sign off the Brief
- [ ] Isolation: test-a / test-b → zero leakage; nothing in `~/.mem0`; each `history.db` holds only its own product
- [ ] Rebuild from `knowledge/` + `facts.yaml` + `learnings.jsonl` → 10 fixed queries, top-5 `source_url` overlap ≥ 0.7; every human fact present verbatim
- [ ] 4 parallel `gtm kb search` + 1 `gtm kb add` → no errors; an automated update of a human fact is refused

---

### Phase 2: ICP, signal catalog, clients & calibrated pre-score *(2 sessions)* → `/gtm-icp`, `/gtm-clients`

**Build**
- `/gtm-icp` proposes:
  - 2–4 segments with KB evidence
  - personas: literal titles, **required `seniority`**, pains, buying trigger
  - anti-ICP and `geo_policy`
  - the **signal catalog** for this product (B2B, §5.1), each code with a plain-language meaning, cost class, threshold, confidence floor and freshness

  You confirm, reject or edit each item (AskUserQuestion). This writes `product.yaml` and `signal_codes.status`. **Nothing unconfirmed is collected.** `contact` and `health` codes are `system` codes: always confirmed, not rejectable, so failures can never be silenced.
- **`gtm/profile.py`** (built here, used again in Phase 3): see Phase 3 for its behaviour.
- `/gtm-clients`: `clients.csv` (company, domain, contract size, retention) → profiler + `harvestapi/linkedin-company` → firmographic observations → fingerprint → `client_pattern` + `clients` table.
- `score.py` pre-score; `tests/fixtures/calibration.yaml` with 10–20 near-miss negatives + 5 anti-ICP companies; **after the ICP is approved**, record a 50-company ICP-adjacent random sample (Apify company search) as a fixture for the calibration test.
- **Cohort query** (`observations.py`, from Harvey, fixed):
  - must-have / must-not over the *latest* observation per account and code
  - numeric codes compare against `threshold` (Harvey treated any nonzero value as "present")
  - `confidence ≥ min_confidence` enforced (Harvey stored a floor but never read it)

**Exit criteria**
- [ ] `product.yaml` validates; you approved the ICP and the catalog
- [ ] Leave-one-out: ≥ 80% of clients ≥ the 80th percentile of a 50-company ICP-adjacent sample; near-misses ≥ 15 points lower on average; anti-ICP all < 40
- [ ] Cohort tests: numeric threshold, confidence floor, latest-value, must-not

#### 5.1 Starter B2B signal catalog (proposed per product; you confirm)

| kind | codes |
|---|---|
| intent | `ASKS_FOR_SOLUTION`, `COMPETITOR_PAIN`, `SWITCHING`, `HIRING_FOR_PROBLEM` |
| trigger | `HIRING_ROLE`, `FUNDING`, `LEADERSHIP_CHANGE`, `LAUNCH`, `EXPANSION`, `SITE_CHANGE`, `PRICING_CHANGE`, `TECH_ADDED`, `TECH_REMOVED`, `RUNNING_GOOGLE_ADS`, `RUNNING_META_ADS`, `EXEC_POST_TOPIC` |
| firmographic | `INDUSTRY`, `SIZE_BAND`, `HQ_COUNTRY`, `TECH_STACK`, `SITE_PAGE_COUNT`, `COMPETITOR_CUSTOMER` |
| contact | `TEAM_MEMBER_FOUND` (name only counts if a title follows), `DECISION_MAKER_TITLE`, `EMAIL_PATTERN`, `EMAIL_STATUS`, `GENERIC_INBOX` |
| health | `SITE_UNREACHABLE`, `PROFILE_FAILED`, `SOURCE_THROTTLED` (**failures are recorded as observations**; Harvey only claims this) |

---

### Phase 3: Account-first engine *(2 sessions)* → `/gtm-accounts`, `gtm collect triggers`

**Build**
- `/gtm-accounts`: TAL from an ICP company search (Apify), competitor-customer pages and client lookalikes.
  - `--estimate` prints the projected Apify cost first, and nothing runs until you confirm (from Harvey)
  - junk/directory filter by pattern
  - dedupe by domain, then by LinkedIn URL, then by normalised name + country
  - pre-score → Tier 1/2/3; refresh weekly within `tal.build_budget_usd`
- **`gtm/profile.py` behaviour** (built in Phase 2; adapted from Harvey `collectors/profile.py`, B2B word lists):
  - fetches homepage, robots, sitemap, and team/careers/contact pages
  - strips scripts before regex; records tech, ad pixels, schema, hiring roles, named people (name + following title), and printed generic inboxes, each with confidence and evidence URL
  - a site that can't be reached → a `SITE_UNREACHABLE` observation
  - re-profiles each account every `profile_refresh_days`
- **Change detection** (Harvey has this only on its roadmap): diffing the latest vs previous observation per code produces trigger observations (`TECH_ADDED/REMOVED`, a new `HIRING_ROLE`, a new exec, `SITE_CHANGE`/`PRICING_CHANGE` via wigolo `diff`).
- Jobs and news triggers per the watermark `last_collected_at`. `budget.py` applies the per-run charge cap and per-actor cost; cost.json includes the Apify account usage before and after.

**Exit criteria**
- [ ] Fixtures: TAL + profile + diff produce valid observations; re-run adds no duplicates; watermark advances; a site that can't be reached yields `SITE_UNREACHABLE`
- [ ] Live: TAL ≥ 100 accounts; one trigger run is within budget; cost.json matches the Apify console
- [ ] Spot-check 20 triggers: ≥ 70% real and relevant

---

### Phase 4: Signal-first engine *(1–2 sessions)* → `gtm collect signals`

**Build**
- LinkedIn post search (confirmed keywords), X named accounts, Instagram (opt-in), Reddit/HN via last30days → `pain_language` only.
- Each `Post` gets `resolvability`: `named_person` only with a real name **and** employer evidence; otherwise `anonymous`, which never becomes a lead.
- Intent labelling: isolated calls, batched, anchored 1–3 rubric with per-product few-shot examples, `<untrusted>` blocks. Drops vendor self-promotion, job seekers, memes. Writes intent observations.
- Golden set: ~100 labelled posts per product; precision checked on any prompt or model change.

**Exit criteria**
- [ ] Golden-set precision ≥ 0.8 for `ASKS_FOR_SOLUTION` and `COMPETITOR_PAIN`
- [ ] No `anonymous` post can create an account or lead (test)
- [ ] Resolvable share recorded in the funnel

---

### Phase 5: Resolve & enrich *(1–2 sessions)* → `gtm resolve`, `gtm enrich`

**Build**
- Resolution uses explicit evidence only, with the evidence URL required. The isolated LLM may *extract* an employer from evidence text but never guesses.
- Pre-score + global ledger + account-navigation check **before** any paid call → top N (§4.2 formula).
- Personas at the account: profiler team page first (free), then Apify employees/profile search. Persona match.
- **Email (`email_finder.py`, adapted from Harvey, bug fixed):**
  - learn the company's pattern from addresses printed on the site, a known verified address, or the Snov domain search; then build ONE candidate and verify it with Snov (async, with timeout)
  - `email_patterns` rows store confidence + source with a **TTL**, and a higher-confidence source always replaces a lower one (Harvey cached its 0.3 default pattern forever)
  - generic-inbox fallback per §4.1; honest `email_class`
- One primary contact per account. `Step.external_ref` is saved before waiting.

**Exit criteria**
- [ ] Resume test: kill during enrich → resume re-uses the Apify run id / Snov task; no duplicate cost
- [ ] Pattern tests: a default pattern is upgraded once a better source appears; the TTL expires
- [ ] Live: emailable% and catch-all% recorded; spend ≤ budget; **funnel go/no-go (§4.4) passes**

---

### Phase 6: Qualification graph *(1 session)* → `gtm qualify`

**Build:** §4.3 in `qualify.py`, holdout flagging, `scored.jsonl`.

**Exit criteria**
- [ ] `run_seed` fixture: planted competitor, existing client, stale signal and a prompt-injection post → all dropped; no reviewer raises a score; ≤ 7 isolated calls
- [ ] Live: you agree with ≥ 80% of kept leads

---

### Phase 7: Write, review, outbox, send & suppression *(2–3 sessions)*

**Build**
- **Playbooks** (`playbooks/*.md`, editable). Our consolidated rules: §5.2 plus the useful parts of Harvey's skills (adapted, attributed). The writer loads **only the relevant sections** (Harvey concatenated about 900 lines into every call).
- **A playbook lint test** fails if any banned phrase appears in a playbook's own examples, or if numbers conflict between files (Harvey's files contradict each other).
- `gtm/writer.py` (isolated):
  - a **FACTS-only prompt** built from the lead's top observations: ONE primary signal by `rank`, one proof, product facts
  - 3-step sequence; `reference_policy` (§3) decided in code from how the signal relates to the recipient:
    - `self_public_professional`: the lead wrote it on LinkedIn or a named X account. Reference the topic, not the act of watching.
    - `company_event`: reference the implication ("teams scaling a CRO function usually hit…").
    - `theme_only`: colleague-authored or anonymous. Pain theme only, no source mention.
  - ≤ 3 attempts per lead (an outage doesn't count); campaign-level and contact-level feedback notes are scoped separately
- Humanize pass (isolated, opt-in). It is accepted only if merge variables are unchanged, no em dash was added, it grows by ≤ 2 words and there is no meta-text (Harvey's acceptance checks).
- **`validate.py` (code; runs at write time, at edit time and again at push time):**
  - word caps **75 / 75 / 40 enforced** (Harvey only prompted for them; its gate allowed 220)
  - subject 2–5 words, lowercase, no name, no "quick question", no fake `Re:`/`Fwd:`
  - email 1 doesn't start with "I"/"We"; no `!`; exactly one question in email 1
  - no links in email 1, ≤ 1 link across the sequence; no HTML
  - unrendered merge tags in any style (`{{x}}`, `{x}`, `[FIRST_NAME]`, `%x%`)
  - banned-phrase list
  - `reference_policy` rules: no Reddit/Instagram/subreddit mentions; no "your post" unless the lead is the signal author; no time words ("recent", "just saw", "this week") in steps 2–3
  - every claim maps to a `proof_id`/`facts.yaml` quote; no URL outside the allowlist; no signal quote > 15 words
  - recipient matches the DB record
  - footer: sender identity, opt-out line, postal address. **A missing postal address *holds* the send** (Harvey only warns). The opt-out line may not contain links/`@` and must use a reply word the handler recognises (from Harvey).
  - `geo_policy` notice text present where required
- **`gtm review` (TTY + PIN at session start, Harvey's "decisions desk" in a terminal UI):**
  - one sequence at a time, with its evidence beside it: observations + URLs, how the email was found and verified, the score breakdown
  - keys: `a` approve · `e` edit (re-validated, new hash, stays pending) · `r` reject + note (feeds the writer) · `j`/`k` move
  - **No approve-all.** Optional `--batch-min-score 90`: shows the count and every subject line, and requires typing the count to confirm.
  - writes an `Approval` row with the content hash and `edited` flag; also reviews `ReplyDraft`s (Phase 9)
- **Outbox → Snov (`outbox.py`, Harvey's ladder plus hardening):**
  - unique `(lead_key, seq_id)`
  - atomic claim: `UPDATE … SET status='claimed' WHERE status='approved' AND content_hash matches`
  - **re-read + re-run every gate after the claim** (suppression, freshness, navigation, mailbox ready/ramp/bounce, kill switch **checked before each push**, not once per cycle)
  - push; on an exception mark `failed_maybe_sent` and **never auto-retry** (look it up in Snov first, by hand)
  - rows stuck `claimed` > 15 min → failed
  - the whole sequence goes as Snov custom fields if supported (Phase 0 answer); otherwise email 1 is personalised and follow-ups are shared templates
- **Replies-before-send (the safety part of the handler, built HERE, before any real prospect is emailed):** step 1 of every `gtm run` pulls Snov replies, bounces and unsubscribes.
  - our own sent text and footers are stripped, then **keyword opt-out detection** runs (any opt-out wording incl. bare `STOP`/"no thanks") → global suppression + sequence `opted_out`
  - any reply → sequence `replied` (classification and drafts come in Phase 9)
  - bounces → suppression + the **rolling 7-day per-mailbox bounce brake**: above 3% the mailbox pauses
  - **if the pull fails, nothing is pushed this run** (from Harvey)
  - a new suppression stops the prospect in every Snov campaign through the API
- `gtm sending pause` (anyone) / `gtm sending resume` (PIN-signed): the global kill switch. **Pausing (global or mailbox) also pauses the matching Snov campaigns through the API**, so scheduled steps 2–3 stop too. If the API can't, the command prints the exact campaigns to pause by hand, and every report shows them until you confirm.
- Real prospects are emailed only after all Phase 7 exit criteria pass.

**Exit criteria**
- [ ] Validator suite: an invented claim, Reddit mention, foreign URL, long quote, 80-word email 1, `!`, two questions, a link in email 1, a missing notice, a time word in step 3, `[FIRST_NAME]` → all rejected
- [ ] Approval tests: an approval row inserted directly into SQLite (no valid signature) → no push; a changed footer/postal address after approval → no push; `gtm review` refuses without a TTY or with a wrong PIN; an edit after approval → no push; a re-write → new hash → no push; a changed `Lead.email` after approval → hash mismatch → no push; ingesting `approved` → impossible (no code path)
- [ ] Mailbox tests: two products sharing `mb_1` together never exceed its effective cap in touches on any day; a mailbox inside `warmup_min_days` pushes nothing
- [ ] Reply-safety tests: a reply quoting our footer is not an opt-out; a bare "STOP" is; a bounce burst over 3% pauses that mailbox (and its Snov campaign) before the next push
- [ ] Outbox tests: a double claim from two processes → one push; an exception → `failed_maybe_sent`, not retried; a failed reply pull → zero pushes
- [ ] Live: 5 sequences to your own addresses render correctly in Snov with tracking off; reply "no thanks" to one → suppressed in every product and its Snov follow-ups stopped; `gtm sending pause` → the Snov campaigns are paused
- [ ] Race test: two products claiming leads at the same account at the same moment → only one sequence is created
- [ ] India: `gtm` refuses IN leads until `geo_policy.IN.legal_signoff` is set

#### 5.2 Writing rules (seed for `playbooks/writing.md` and `signals.md`)
- Deliverability > list relevance > copy. Write like a real person sending a real email.
- One observation, one question. **A wrong specific is worse than a right generic.**
- Signal rank for the opener: stated intent > hiring > funding > tech change > tech present > industry. Use ONE; stacking reads like a dossier.
- Hiring: talk about the workload behind the hire, not the hire itself. Name at most one tool; never list their stack back at them.
- No pitch and no meeting ask in email 1. Use an easy-yes question ("is this on your radar at all?").
- The breakup email (step 3) doesn't pitch or guilt-trip.
- No open/click tracking.
- If a prospect asks whether this is automated, answer **truthfully**. The validator must not block truthful disclosure in replies (Harvey's gate does).

---

### Phase 8: Run lifecycle & scheduling *(1 session)* → `gtm run`, `gtm schedule install`, `/gtm-run`, `/gtm-report`

**Build**
- `gtm run --product P --mode full|refresh|sync|noop`:
  - takes the product lock
  - **resume rule (modes `full`/`refresh` only, matched by product + mode):** if the latest run *of the same mode* is `running` (dead process) or `failed` with `attempt < 2` and started **within the last 24 h**, resume it (`attempt+1`; steps 3–7 that are done are skipped, but **steps 1–2 always re-run**, so a fresh reply sync happens before any drain); otherwise start a new run. `sync` and `noop` runs are never resumed and never resume others.
  - executes §1.1 steps 1–8 in order
  - **finish:** `done`, or `partial` when a budget cap was hit (closed, not resumed), or `failed` with a reason
  - always writes `report.md` + `cost.json`, releases the lock, exits non-zero on failure
  - quiet hours: no pushes outside `sending.window` (Snov also schedules within the recipient's business hours)
- `refresh` mode = steps 1–2, then step 3 limited to LinkedIn posts and jobs triggers, then the full 4 → 5 → 6 → 7 for new items, within the remaining daily cap. Enrich and qualify are never skipped.
- `gtm schedule install --product P` writes user crontab entries from `schedule.runs[]`, plus **hourly** `gtm run --mode sync` (steps 1–2 only: replies, suppression, outbox drain). The **daily** `gtm feedback` entry is added in Phase 9 (`gtm schedule install --feedback`). User crontab means no linger or sudo is needed.
- `/gtm-run` (manual trigger) and `/gtm-report` skills. **Reports contain only aggregates, signal codes, scores, ids and evidence *domains*.** Company/person names are sanitised (≤ 60 chars, letters/digits/basic punctuation) and shown in a data table. Raw scraped text and reply bodies never appear (those are only in `gtm review`, in your own terminal), so untrusted text doesn't enter the interactive session (§0 #2). `gtm kb search` redacts `pain_language` for the same reason.

**Exit criteria**
- [ ] 2 scheduled runs + hourly syncs complete unattended (also while logged out); reports written
- [ ] Kill mid-enrich → the next run resumes the same run_id, no duplicate cost; a budget-capped run is closed as `partial`, and the next cron starts a new run
- [ ] Two products at the same minute → both succeed; isolation green; a suppression in one is respected by the other
- [ ] An injection post in a scheduled run → zero pushes without approval (isolated LLM + hash gate)

---

### Phase 9: Replies, conversations & learning *(1–2 sessions)* → `gtm feedback`, `/gtm-report`

**Build**
- **Handler (adapted from Harvey, with fixes):**
  - dedupe by provider id
  - reuses Phase 7's stripping, keyword opt-out and bounce brake (already live)
  - **keyword-first escalation before the LLM:** legal/angry/contract words → `needs_human`
  - then isolated classification: interested / not now / referral / objection / OOO / bounce
  - classifier failure → `needs_human`
- **Reply drafts** (`ReplyDraft`, hash-bound `Approval`, reviewed in `gtm review`); sent through the Snov API if Phase 0 found an in-thread reply endpoint, otherwise marked `sent_manual` after you send it from the mailbox. Objection playbook (diagnose: reflex / condition / smokescreen / genuine; reply shorter than theirs; one attempt; opt-outs are never objections); conversation stages; referral → new lead.
- `gtm schedule install --feedback` adds the daily feedback cron.
- Analytics: north star + guardrails by source / persona / segment / holdout, with confidence intervals; weekly proposals past the §4.5 threshold; aggregate `learning` memories; `learnings.jsonl` export.

**Exit criteria**
- [ ] A contract/legal question → `needs_human`; an objection gets a draft in `gtm review`; a referral creates a `referral=true` lead
- [ ] Below-threshold segments produce no proposal and no memory

---

### Phase 10: Later (optional)
- Local dashboard (Harvey-style: loopback only, token, Host/Origin checks) or an Artifact dashboard
- Precleared integration
- LinkedIn manual-assist (never automated)
- A VPS for always-on runs
- Consent-only voice (Harvey's reasoning stands: AI cold-calling is a TCPA risk)

---

## 6. Compliance, security & deliverability

### 6.1 Compliance (`docs/compliance.md`; legal review before the first real send)
- **Per-geo policy** in `product.yaml.geo_policy` with notice text; unknown = blocked.
- **EU/UK:** legitimate interest + written LIA + Art. 14 notice; blocked until the LIA exists. Germany needs prior consent even for B2B email.
- **India (DPDP Act 2023 + Rules 2025):** consent-based, with no general legitimate-interest ground. Allowed only with notice + data-source statement + easy withdrawal, **after** `legal_signoff`.
- **US CAN-SPAM:** accurate headers, honest subject, postal address (required, see the validator), opt-out honoured immediately.
- **Phones:** manual follow-up only, no automated calls or SMS, respect TRAI DND.
- **Erasure:** `gtm forget <email>` across all products: SQLite rows, run files redacted, mem0 by `lead_key` (+ history.db), Snov prospect deleted, hashes added to **global suppression**, `erasure_log`.
- **Retention:** run folders 90 days; Apify datasets unnamed; backups encrypted with the same retention; low `cleanupPeriodDays`.

### 6.2 Security
- Secrets only in `~/.config/gtm-tools/env` (600), read by `gtm` itself.
- Untrusted text goes only to isolated calls, inside `<untrusted>` blocks, truncated, with control characters stripped.
- `.claude/settings.json` (interactive sessions):
  - **deny** Snov/Apify MCP write/run tools (exact names from Phase 0) and `Bash(uv run gtm review*)`, `Bash(uv run gtm autosend*)`, `Bash(uv run gtm forget*)`, `Bash(uv run gtm sending*)`, plus `Read`/`Bash` access to `~/.config/gtm-tools/**`
  - no `gtm` command available to the session prints raw scraped text or reply bodies
  - **allow** other `Bash(uv run gtm *)`, wigolo tools
  - `gtm review` additionally refuses to run without a TTY
- `public_http.py` for every fetch of a prospect's site (SSRF-safe).
- **Deny for Claude sessions:** `Read`/`Edit`/`Write`/`Bash` on `~/.config/gtm-tools/**` and `~/.local/share/gtm-tools/**`, and `Bash(*sqlite3*)`.
- **Threat model (honest):**
  - *Untrusted text* (scraped posts, sites, replies) is quarantined in tool-less isolated calls (§1.3).
  - *An over-eager or confused Claude session* can't approve, resume, enable auto-send or forge a sendable row: those all need your PIN-encrypted signing key.
  - *Anything running as your Linux user with full shell access* could in principle tamper with local data or wait for you to type your PIN. The signatures make that much harder; they don't make it impossible. Keep the PIN out of chats and files.

### 6.3 Deliverability
- A dedicated sending domain; SPF/DKIM/DMARC; **`gtm warmup` must be READY or the mailbox can't send** (Harvey only advises).
- **Ramp per mailbox** (from Harvey): no cold email for `warmup_min_days` (14) from `warmup_start`, then `start_sends` (5) + `daily_increase` (3)/day up to `daily_cap` (30).
- Verified addresses only; catch-all per §4.1; 7-day bounce > 3% → that mailbox pauses.
- Snov "stop after a company reply" ON; tracking OFF; send in recipient business hours.
- **Auto-send** (default off): `gtm autosend enable --product P` (TTY + PIN, denied to Claude) succeeds only if **code-computed** eligibility holds from the `approvals` table and ledger: ≥ 14 days since first push, ≥ 50 reviewed sequences, ≥ 90% approved with `edited=false`, bounce < 2%, no spam complaint. Once enabled in `contacts.db`, `gtm run` writes `Approval(via=policy)` only for validated sequences with score ≥ `auto_send_min_score` (85). Every gate still applies. It disables itself automatically if any guardrail goes red. Nothing in `product.yaml` can turn it on.

---

## 7. `product.yaml` (shape; validated by pydantic `ProductConfig`)

```yaml
product: { slug: acme, name: Acme, url: https://acme.com, mcp: null }
icp:
  segments:
    - { name: D2C e-commerce, industries: [e-commerce, retail], size_band: [51-200, 201-1000],
        geos: [IN, US], tech_stack_any: [Shopify, Magento] }
anti_icp: [agencies, students, competitors]
personas:
  - { name: Founder/CEO, titles: [CEO, Founder, Co-founder], seniority: c_level }
  - { name: Head of Growth, titles: [Head of Growth, VP Marketing, CMO], seniority: vp }
geo_policy:                    # keys: ISO-3166 country codes, or groups expanded in code (EU = 27 member states, EEA)
  IN: { status: allowed_with_notice, legal_signoff: null,
        notice: "We found your details on <source>; reply 'stop' and we'll delete them." }
  US: { status: allowed }
  EU: { status: blocked }
  UK: { status: blocked }
  default: { status: blocked }
signals:                       # confirmed codes live in state.db signal_codes; this holds inputs
  linkedin: { keywords: ["cart abandonment"], company_posts: true }
  x: { queries: [], named_accounts_only: true }
  instagram: { enabled: false }
  pain_language: { subreddits: [ecommerce, shopify], hn: true }
  hiring_keywords: ["growth marketer", "CRO"]
  competitors: [CompetitorA, CompetitorB]
tal: { refresh: weekly, max_accounts: 500, build_budget_usd: 10, max_items_per_query: 200,
       profile_refresh_days: 30 }
scoring:
  weights: { icp_fit: 30, intent: 30, lookalike: auto, authority: 10, contactability: 10 }
  prescore_cutoff: 60
  cutoff: 70
  recontact_days: 90
  allow_catchall: false
  holdout_pct: 20
budgets: { apify_usd_per_run: 3, snov_credits_per_run: 60, llm_calls_per_run: 150,
           max_items_per_actor: 100 }
schedule:
  runs: [{at: "09:00", mode: full}, {at: "15:00", mode: refresh}]
  sync: hourly
  feedback: daily
  timezone: Asia/Kolkata
sending:
  auto_send_min_score: 85        # enabling auto-send is NOT a config option (§6.3)
  snov_list: "Acme – Outbound"
  mailboxes: [{id: mb_1, share: 1.0}]
  touches: 3
  followup_days: [4, 12]
  window: { start: "09:00", end: "18:00", tz: recipient }
voice: { tone: "direct, peer-to-peer", banned_extra: [], sender_name: "", postal_address: "",
         opt_out_line: "Not interested? Reply \"no thanks\" and I won't email again." }
```

---

## 8. Decisions needed from you (defaults in **bold**)

**Decided (2026-10-23):**
- **The founder is the user.** He doesn't fill config files. The **`/gtm-setup` wizard** (Phase 0) asks him for everything, one question at a time, in plain language: product URL, clients, target countries, Snov/Apify keys (typed into his own terminal, never into chat), mailbox, schedule and budgets. It saves the answers and re-asks only what is missing. Every later skill checks setup first and sends him back to the wizard if something it needs is missing.
- **Claude plan:** built on Max 20x, **must work on Max 5x**. The wizard asks which plan; `usage.profile` sets the defaults:
  - `max5x`: 60 isolated calls/run, 2 runs/day, qualify graph with 2 reviewers when the cap is tight
  - `max20x`: 150 calls/run
  - any call that hits a usage limit pauses LLM work for 1 hour and the run ends `partial`; it never retries in a loop
- **Countries (plain language):** the wizard asks *"Which countries' companies do you want to email?"* US is allowed by default. **India is blocked until the founder ticks "I've checked Indian data-privacy rules (DPDP Act) with a lawyer".** The reason: Indian privacy law needs a clear notice and a simple way to say "delete my data", and this isn't legal advice. EU/UK stay blocked.

**The wizard asks the founder for:** pilot product name + URL · `clients.csv` (5–20 clients) · 10–20 companies that fit the ICP but never bought · Snov API access + a warmed sending mailbox · budget per run (default **$3 Apify / 60 Snov credits**) · schedule (default **09:00 full + 15:00 refresh + hourly sync, IST**) · Instagram **off**, catch-all **excluded**, Precleared **off** unless he changes them.

---

## 9. Session rules (copy into `CLAUDE.md`)

- Always `--product <slug>`; state the product and run_id at the start of any data work.
- Execution boundary (§1.2): paid, irreversible or stateful actions only via `uv run gtm …`. Untrusted text only through `gtm/llm.py`. Never call Apify/Snov MCP write tools; never edit SQLite directly.
- Never approve, sign, resume sending or enable auto-send. Approval = `gtm review` in the user's own terminal (TTY + PIN, signed); pushing = `gtm run` (signature-verified, code-gated). Never write to SQLite or `~/.local/share/gtm-tools` except through `gtm` commands.
- Runtime data only under `~/.local/share/gtm-tools/`; secrets only in `~/.config/gtm-tools/env`.
- mem0: `infer=False`, no personal data, human facts only via `facts.yaml`.
- Every lead links to observations with evidence URLs; every claim traces to a `proof_id`/`facts.yaml`.
- Never collect a signal code you haven't confirmed. Estimate before any paid batch.
- Build against fixtures first; one live check per phase.
- Global `~/.claude/agents/*` outreach agents are Saksham-tuned. Use the project's own prompts and playbooks.

---

## 10. Order of work

| Phase | Deliverable | Sessions | Depends on |
|---|---|---|---|
| 0 | Repo, schemas, isolated LLM, SSRF client, Qdrant, ledger, tool truth, warmup, cron noop | 2 | — |
| 1 | `/gtm-onboard` + isolated mem0 KB | 1–2 | 0 |
| 2 | `/gtm-icp` + signal catalog, `/gtm-clients`, pre-score, cohorts | 2 | 1 |
| 3 | TAL, site profiler, change-detection triggers | 2 | 2 |
| 4 | Social signals (named people) | 1–2 | 2 |
| 5 | Resolve + enrich (pattern-first email) + funnel go/no-go | 1–2 | 3 (4 optional) |
| 6 | Qualification graph | 1 | 5 |
| 7 | Playbooks, writer, validator, review, outbox → Snov, replies-before-send | 2–3 | 6 |
| 8 | `gtm run` lifecycle, cron, reports | 1 | 7 |
| 9 | Replies, conversations, learning | 1–2 | 8 |

**First useful output:** qualified leads after Phase 6 (account-first path: 3 → 5 → 6). Phase 4 can be built in parallel with Phase 3.

---

## 11. Harvey comparison (verified from source, commit `f851026`)

### 11.1 Adopted (and how we improve on it)

| Harvey idea | Verified evidence | Our version |
|---|---|---|
| Isolated, tool-less `claude -p` on your subscription | `claude_runtime.py:47-55,162-199,244-268`; leak test `tests/test_claude_cli_isolation.py` | §1.3. Same flags (all present in CLI 2.1.309), help-probe, leak test. Used for **all** untrusted text. |
| "Every fact is a row" + vocabulary trigger | `state.py:338-368` | §3 `Observation`/`SignalCode`; trigger also requires `confirmed` |
| You confirm signals before collection | `signals.py`, `collectors/profile.py:239,396-397`, `collectors/discover.py:974` | §5 Phase 2. Harvey filters per code inside its collectors; we *also* enforce it in the database trigger, so no code path can store an unconfirmed signal |
| Cohort as a query | `state.py:1349-1415` | Fixed: numeric thresholds, confidence floor enforced, latest value |
| Free website profiler, agency-style credit confidence, "name counts only with a title" | `profile.py:62-70,318-387` | `gtm/profile.py`, B2B word lists. Failures recorded as observations (Harvey only claims this) |
| SSRF-safe HTTP client | `public_http.py:28-114` | + response-size cap, timeouts |
| Pattern-first email + verify one candidate, honest statuses, generic-inbox rules | `email_finder.py:314-554`, `scout.py:1461-1556` | + TTL and upgrade-on-better-source (Harvey's cache bug fixed); Snov verifier |
| Estimate before spend, kill switch between batches | `discover.py:844-889` | `--estimate` on TAL builds; per-run charge caps |
| Outbox ladder: unique key, atomic claim, re-check, "may have sent = never retry", stuck → failed | `state.py:274-304,980-1014`, `sender.py:643-672` | + approval audit (who/when/how) and **content-hash binding** (Harvey stores neither) |
| Replies read before any send; skip sends if the read fails | `main.py:349-365` | §1.1 step 1 + hourly sync |
| Opt-out keywords override the LLM; our own text stripped first | `handler.py:51-166, 99-136` | Phase 9 |
| Deterministic pre-send gate | `gate.py:68-112` | Much stricter: word caps 75/75/40 in code, one question, no `!`, merge-tag styles, claims trace, reference policy |
| Footer wording checked against the opt-out parser | `config.py:303-345` | Validator; a missing postal address **holds** the send |
| Warmup checks + cold ramp | `deliverability.py:93-216`, `config.py:127-140` | NOT READY **blocks** (Harvey: advisory); ramp per mailbox |
| One-at-a-time review desk | `dashboard.py:891-990` | TTY `gtm review`, no approve-all |
| Editable Markdown playbooks | `skills/*.md`, `brain.py:716` | `playbooks/` + section selection + lint test |
| Writer: FACTS-only prompt, attempt counter ignoring outages, scoped feedback, humanizer checks | `writer.py:99-322`, `humanize.py:57-94` | Phase 7 |
| Deterministic priority loop (replies > send > write > prospect) | `main.py:112-151` | §1.1 step order in `gtm run` |

### 11.2 Not adopted (and why)
- **Local-SMB discovery** (OpenStreetMap, Google listing/review signals, US trade word lists): built for roofers and dentists; we sell B2B.
- **Single product** (one product/ICP/persona, no product id on tables): we need strict multi-product isolation.
- **Trainer's "infer the most likely competitors"** (`trainer.py:690`): invites made-up battle cards. We only use competitors named on the site or by you.
- **Bursty pacing** (8 sends per heartbeat, `sender.py:40`) and **no recipient time zones**: Snov schedules sends in recipient business hours.
- **Approve-all** and approvals without an audit trail: replaced by the review desk + hash-bound approvals.
- **Subject-line bounce guessing; an all-time bounce rate with a counter that resets**: we use Snov's bounce status and a rolling per-mailbox window.
- **Undocumented quota endpoint as a control** (`integrations/quota.py`): our hard control is a per-run call cap; quota readings are informational only.
- **Concatenating every skill into every prompt** (~900 lines): wastes tokens. We select sections.
- **LinkedIn browser automation**: against LinkedIn's ToS; never.
- **Its observations table never reaches the writer.** The writer reads legacy company columns (`companies.signals_json`, hiring/tech only; `writer.py:271-287`), so signals like ad spend or agency never shape an email. Our writer is built *from observations*.

### 11.3 Where this plan goes beyond Harvey
- Multi-product isolation plus a global compliance ledger
- An account-first target account list with change-detection triggers
- Named-person social signals with resolvability
- Lookalikes from your clients, with leave-one-out calibration
- A graph qualification step with a holdout
- Hash-bound approvals
- Validator rules enforced in code
- Per-geo legal policy, including India DPDP
- Erasure across every store
- Fixture-first testing
- Outcome metrics with sample thresholds

---

## 12. Review history
- `docs/plan-review-2026-10-23-findings.txt`: v1's 45 findings (all addressed in v2/v2.1; see `docs/plan-v2.1.md` Appendix A/B).
- v2.1 gate (round 3) → fixed in v3:
  - approval model unified (`Approval` table + `content_hash`, §3)
  - run lifecycle (finish / partial / attempt cap, §5 Phase 8)
  - sequence and reply state (`sequences` in the ledger, §2.3; daily `gtm feedback`, Phase 8)
  - enrichment uses the remaining daily cap (§4.2)
  - `holdout`/`referral` fields; catch-all mailbox flag; `prescore_cutoff`
  - a whole-run lock (§2.1)
  - run-dir permissions are no longer needed (`gtm run` is Python)
- v3 gate → fixed in v3.1:
  - run modes + per-mode resume
  - refresh never skips enrich/qualify
  - profiler moved to Phase 2; calibration sample fixture in Phase 0
  - `--limit` before sending exists; warmup starts in Phase 0
  - auto-send is PIN-gated and code-eligible; the hash covers recipient + lead
  - reports carry no raw untrusted text
  - system signal codes
  - mailbox ids + global per-mailbox counter + shares
  - `ReplyDraft` + send path; Snov stop/delete/reply checks
  - review PIN; referral overrides closure; feedback cron moved to Phase 9; holdout sampled before the graph; freshness names aligned
  - Harvey claims corrected
- v3.1 gate → fixed in v3.2:
  - **signed approvals** (Ed25519 under PIN) over fully rendered emails + mailbox; signed auto-send/resume; review only in your own terminal
  - `ReplyDraft` recipient + outbox ladder
  - touch-based mailbox reservations in one `contacts.db` transaction together with the navigation check (fixes the cross-product race); one Snov campaign per (product, mailbox)
  - resume within 24 h with steps 1–2 always re-run
  - calibration sample recorded after ICP approval
  - opt-out keywords + bounce brake moved into Phase 7 (before real sends)
  - pausing also pauses Snov campaigns
  - minor: sync mode listed; `email_patterns`/`clients`/`watermarks` defined; `gtm mailbox add`; geo keys; sanitised reports; holdout never auto-sent
  - this was the 3rd gate round (cap). See §13 for the threat model and what the review graph did not settle

---

## 13. Review status & open items (read before building)

Review rounds:
- **v1:** 4-lens review + verification, 45 findings → v2
- **v2.1 gate:** FAIL → v3
- **Harvey analysis:** 3 code readers → v3
- **v3 gate:** FAIL (11 major) → v3.1
- **v3.1 gate:** FAIL (1 blocker, 6 partial, 3 major) → **v3.2**

Each round found fewer and narrower issues. The last ones were a forged-approval path and the order in which safety features get built, not the core design.

**Not independently re-verified:** the v3.2 edits themselves. The loop stopped at its 3-round cap, so there was no 4th gate. Treat the v3.2 changes in §12 as *specified but unreviewed*. Phase 0's tests (signature check, leak test, race test, outbox tests) are where they get proven in code.

**Open items for the build sessions (none block Phase 0):**
1. Snov API facts (custom-field limits, campaign pause, prospect stop/delete, in-thread reply, sender per campaign) are unknown until Phase 0. Several Phase 7 designs have stated fallbacks if the API can't do something.
2. The wigolo MCP stdio client from Python is unverified until Phase 0. Fallback: Apify search.
3. Subscription limits for ~150 isolated `claude -p` calls per run (§8 #7).
4. Legal review for India before live sends.
