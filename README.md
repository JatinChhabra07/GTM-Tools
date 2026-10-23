# GTM-Tools: the outreach automation, in plain English

**What it does:** you give it your product's website. Every day it finds 10–15 companies and
people who show a real sign of needing your product, checks their email addresses, and writes a
short personal email to each. **Nothing is sent until you approve it with your PIN.**

## How it works (one day)

1. **Learns your product** from your website and keeps a separate memory per product
   (mem0). Products never mix.
2. **Learns your ideal customer** from you and from your existing clients: "Snapdeal uses us, so
   find companies like Snapdeal."
3. **Watches for buying signals** two or three times a day:
   - companies hiring for the problem you solve, raising money, changing leaders, tools, pricing
     or website (Apify, wigolo, website checks)
   - named people on LinkedIn/X asking for a solution or complaining about a competitor
   - Reddit / Hacker News (last30days) and, if you switch them on, Quora, Indie Hackers, forums,
     review sites and job boards (free web search). They teach it how buyers describe the pain,
     and anonymous people asking for a solution go to your **intent sheet** (below); those
     posters are never emailed. A post that names the poster's company adds that company as a
     target; the right person there is found the normal way.
   - `/gtm-icp` first tests each source for your product and suggests which to switch off
     ("X finds nothing for this product" is a normal answer).
4. **Finds the right person** at each company (Apify, their own website) and **checks the email
   address** (Snov.io). The company's own published phone line is shown in your review screen for
   a manual call if you want one; nothing is dialled automatically.
5. **Filters hard:** a panel of AI reviewers (the graph-engineering pattern) throws out weak,
   risky or wrong leads. 10 great leads beat 50 random ones.
6. **Writes the emails:** short, human, one specific reason to reach out, only facts that can be
   traced to your website or to you.
7. **You review** in your terminal (`bin/gtm review`) and approve with your PIN.
8. **Snov.io sends** them, follows up twice, and stops the moment anyone at that company replies.
9. **Learns from replies:** unsubscribes are blocked forever, referrals become new leads, reply
   drafts wait for your review, and what works is reported back with real numbers.

## One-time setup (about an hour, plus two weeks of mailbox warm-up)

Open Claude Code in this folder (`cd /mnt/windows/GTM-Tools && claude`) and type **`/gtm`**. It
tells you where you are and walks you through each step. In order:

| step | where | what |
|---|---|---|
| 1 | Claude Code | `/gtm-setup`: installs the tools, creates your product, asks about countries, sender name and address, budget, schedule |
| 2 | **your terminal** | `bin/gtm keys init`: creates your approval PIN |
| 3 | **your terminal** | `bin/gtm secrets set`: your Apify and Snov.io API keys (Firecrawl optional) |
| 4 | Claude Code | `/gtm-onboard`: reads your website into the knowledge base; you correct it |
| 5 | Claude Code | `/gtm-icp`: who to target, who gets the email, which signals to watch |
| 6 | Claude Code | `/gtm-clients`: your existing clients and lost deals (for lookalikes) |
| 7 | Claude Code | `/gtm-accounts`: builds the list of target companies (optional: `/gtm-competitors`, see below) |
| 8 | **your terminal** | `bin/gtm mailbox add --id mb_1 --address you@your-sending-domain.com --warmup-start YYYY-MM-DD` |
| 9 | Claude Code + Snov.io | `/gtm-setup` again: checks your sending domain, creates the Snov list, walks you through the 5-minute campaign setup in Snov.io, links it |
| 10 | Claude Code | `/gtm-run`: a first test run; then `/gtm-setup` installs the daily schedule |

Never paste a key or your PIN into chat. If you do by accident, replace that key in the service.

**Use a separate sending domain** (e.g. `getacme.com`, not your main domain) and let the mailbox
warm up for about two weeks before real volume. The system starts at 5 emails a day and grows
slowly on its own.

## Competitors (optional)

`/gtm-competitors` writes a short brief per competitor (what they sell, to whom, prices, what
reviewers complain about), every line linked to the page it came from, into
`products/<slug>/knowledge/competitors/`. It then adds companies that use a competitor as target
accounts: free from the competitor's customer and case-study pages and from its script on a
company's website; paid (G2/Capterra/Trustpilot reviews, LinkedIn/X posts) only after it shows
the price and you say yes. The competitor itself is never emailed. Full runs re-check competitor
pricing and customer pages once a week for free.

## Every day

- **Review** (5–10 minutes): `bin/gtm review --product <slug>` in your terminal. For each email:
  `a` approve, `e` edit, `r` reject (say why: it learns), `j`/`k` next/previous, `q` quit.
  Reply drafts: `bin/gtm review --product <slug> --drafts`.
- **How is it going?** `/gtm-report` in Claude Code.
- **Something wrong?** `/gtm-doctor`.

## Intent sheet

Most Reddit, Hacker News and forum posts are anonymous, so they can't be emailed. When one shows
a real need, the run adds a row to `~/.local/share/gtm-tools/<slug>/intent_sheet.csv` (opens in
Google Sheets or Excel): date found, source, post link, the poster's profile link when the source
gives one (Hacker News does; Reddit usually doesn't, so open the post), what they need in one
line, and their company if they named it. It is for **your manual follow-up** (a helpful reply
in the thread, say). **Nobody on the sheet is emailed or contacted automatically.**
`bin/gtm sheet --product <slug>` shows the new rows; rows older than 90 days are dropped; if
someone asks to be removed, `bin/gtm sheet --product <slug> --remove <their post or profile link>`
deletes them and keeps them out.

## Emergency stop

`bin/gtm sending pause` stops all sending immediately (every product, every mailbox) and pauses
the Snov campaigns. Resuming needs your PIN: `bin/gtm sending resume` (your terminal).

## Commands only you can run (your terminal, they refuse inside Claude Code)

`keys init` · `secrets set` · `review` · `mailbox add` · `sending resume` · `autosend enable` ·
`forget <email>` (erase one person everywhere, e.g. after a deletion request)

## Safety built in
- No email goes out without your PIN-signed approval. If an approved email, its recipient, footer
  or mailbox changes afterwards, it is not sent. Auto-send can only be switched on later by you,
  after a proven track record.
- The AI that reads scraped web content and replies has no tools, so a malicious post can't make
  it do anything.
- Unsubscribes, bounces and "not interested" replies are honoured across all products, forever.
- Spending caps on every run (default $3 Apify, 60 Snov credits). Bounce rate above 3% pauses the
  mailbox automatically.
- India stays blocked until you confirm legal advice; EU/UK stay blocked.
- Works on a Claude Max 5x plan (fewer AI calls per run) or Max 20x.

## Where things live
- Settings per product: `products/<slug>/product.yaml` (change them through the skills, not by hand)
- Your clients and facts: `products/<slug>/clients.csv`, `facts.yaml` (not committed to git)
- Data, run reports and logs: `~/.local/share/gtm-tools/` · keys: `~/.config/gtm-tools/` (private)

The full technical plan is in `plan.md`; tool details in `docs/tools.md`; compliance notes in
`docs/compliance.md`.
