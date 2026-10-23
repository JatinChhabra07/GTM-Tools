# Compliance notes (draft: get legal review before the first real send)

This is an engineering record of what the system enforces in code, written so a lawyer can check it
quickly. It is not legal advice.

## Who is emailed, and on what basis

| region | default | what the code enforces | what a human must do |
|---|---|---|---|
| United States (CAN-SPAM) | allowed | sender identity, honest subject (no `Re:`/`Fwd:` tricks, validator), physical postal address in every email (validator refuses sequences without it), one-line opt-out honoured immediately and permanently | provide a real postal address in `/gtm-setup` |
| India (DPDP Act 2023 + DPDP Rules 2025) | **blocked** | can only be enabled with `legal_signoff` text (who advised, when) **and** a notice; the notice and data source go into every email to an Indian lead; "stop"/"delete" replies erase and suppress | get advice; record it in `/gtm-setup` |
| EU / UK (GDPR, PECR) | **blocked** | config refuses `allowed` without the legitimate-interest assessment workflow, which is not built | do not enable |
| unknown country | **blocked** | a lead whose country can't be determined is never emailed | none |

Geography is decided per lead from the person's location, then the company's HQ; a country-code
domain (`.in`, `.de`...) can only add a block, never unblock.

## Data minimisation
- Collected per lead: name, title, employer, work email (verified), LinkedIn URL, the public post or
  page that triggered outreach (evidence URL).
- Phones: only the company's own published main line (a `tel:` link on its home, contact or about
  page) is stored, and it is only shown in the founder's review screen for **manual** follow-up.
  No personal mobile numbers are collected; nothing dials or texts (respect TRAI DND in India).
- People who only appear in Reddit/Hacker News/forum/review-site discussions (and Instagram) are
  **never** leads and are never emailed: no lead row can be created without a real name and an
  employer written by the author, and these posts are stored without an author name. Their text is
  used in aggregate as "pain language" vocabulary inside isolated calls and is not shown in reports.
- **Intent sheet** (`<data dir>/<slug>/intent_sheet.csv`, file mode 0600): when such a post shows a
  buying need, one row is kept for the founder's **manual** follow-up: date found, source, post URL,
  the author's public profile URL only when the source itself gives a username (Hacker News; the
  pinned last30days engine gives no Reddit author), the intent code, a one-line paraphrase written
  by the isolated labeller (no quotes; dropped if it contains an email address or phone number) and
  the company only if the post names it. Nothing is sent to anyone on it. Cells are neutralised
  against spreadsheet formulas.
- A post that names the poster's company may make that **company** a target account (evidence: the
  post URL). The poster stays anonymous; the email goes to a decision maker found by normal
  enrichment, and the writer may use the pain theme only, never where it was seen (`theme_only`).
- The knowledge base (mem0) refuses text containing an email address or phone number.
- Reports and Claude sessions see aggregates only: no email bodies, addresses or reply text.

## Opt-out, suppression and erasure
- Opt-out phrases in a reply (after stripping quoted text and our own footer) suppress the address
  in the **global** ledger (all products), add it to Snov's do-not-email list, and set it to
  Unsubscribed in every campaign. Bounces are suppressed too.
- `bin/gtm forget <email>` (founder's terminal): deletes the person's rows in every product,
  redacts run files, deletes their knowledge-base memories and mem0 history rows, adds email and
  LinkedIn hashes to permanent suppression, logs the erasure, and blocks them in Snov. **Snov has no
  API to delete a prospect**, so a manual action ("delete from every Snov list") is queued and shown
  in `bin/gtm manual list` until done.
- Intent sheet: `bin/gtm sheet --product <slug> --remove <post or profile URL>` deletes the matching
  rows and stores only a hash of the URL so the row is never added again.

## Retention
- Run folders and cron logs: deleted after 90 days (daily feedback pass).
- Intent sheet rows: dropped 90 days after the date they were found (same daily pass); the file
  itself is kept.
- Apify datasets are unnamed (Apify's own default expiry applies).
- Suppression hashes and the erasure log are kept indefinitely (they exist to keep people out).

## Sending discipline (deliverability and consent hygiene)
- Every sequence needs the founder's PIN-signed approval; a change to content, recipient, footer
  or mailbox after approval voids it.
- Separate sending domain; SPF/DKIM/DMARC and blocklist checks must pass (`gtm warmup`); 14-day
  warm-up, then 5/day rising by 3/day to the cap; open/click tracking off; one company is contacted
  through one person at a time; a reply from anyone at the company stops the sequence; 90-day
  closure after a reply, opt-out or bounce.
- 7-day bounce rate above 3% pauses the mailbox automatically.

## Platform terms
LinkedIn, X and Instagram data comes through Apify actors that read public pages. Their terms of
service restrict automated collection; the founder accepts that risk by enabling those signals in
`/gtm-icp`. The system collects only what the confirmed signals need, at low volume, and never logs
in as the founder. The fallback sources (Quora, Indie Hackers, forums, review sites, job boards,
Hacker News via web search) are read only as public search-result titles and snippets (wigolo), and
only when the founder switches them on.

## Open items for legal review
1. India: wording of the notice and the data-source statement; whether B2B work emails change the
   analysis.
2. Whether the referral flow (a reply naming a colleague creates a new lead) needs extra wording.
3. Retention periods above.
