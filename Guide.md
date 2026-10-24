# GTM-Tools: Founder Guide

Cold-email outreach that finds good leads, writes personal emails and sends them only after you approve.
Talk to it in Claude Code with `/gtm` (or say "follow Guide.md"). It asks you for everything it needs.
A few steps are yours only (keys, PIN, approving emails). Claude stops and tells you the exact command to type in your own terminal.

## Before you start
- You need Claude Code (Max 5x or higher), Docker running, and an Apify key and a Snov.io key.
- **Never paste keys or your PIN into the Claude chat.** Type them only in your own terminal.

## One-time setup (about 30 min)
1. Open Claude Code in this folder and run `/gtm-setup`. It installs everything (mem0, Qdrant, wigolo, last30days).
2. In **your own terminal**, run:
   - `bin/gtm secrets set` to add your Apify and Snov keys
   - `bin/gtm keys init` to create the PIN you approve emails with
3. Run `/gtm-onboard`. It reads your product website into the product's own knowledge base.
4. Run `/gtm-icp`. It suggests who to target (ICP, roles like CEO, HR or co-founder) and which signals to watch. You confirm or edit.
5. Run `/gtm-clients` to add your current clients, so it can find similar companies. Skip this if you're a startup with none yet.
6. Optional: run `/gtm-competitors` to analyse your competitors and find their customers.
7. Run `bin/gtm warmup --product <slug>` to check your email domain is ready to send (SPF, DKIM, DMARC).

## Test before going live
1. Run `bin/gtm tools check --paid` to check the keys work (free, uses no credits).
2. Run `/gtm-run` for a small first run. It shows the cost estimate before spending anything.
3. Run `/gtm-report` to see what it found.

## Daily use
- **Runs:** `/gtm-run` sets the schedule (2–3 times a day, or the times you pick). Each run finds leads, shortlists the best ~15, finds their contacts, checks them and writes emails.
- **Approve emails** in your own terminal: `bin/gtm review --product <slug>`. Nothing is sent without your PIN.
- **Replies:** `bin/gtm review --product <slug> --drafts` to approve reply drafts.
- **Intent sheet:** `bin/gtm sheet --product <slug>` lists people who posted buying intent on Reddit or other forums, for you to follow up by hand. Nobody on it is emailed.
- **Results:** `/gtm-report`.
- **Something broken:** `/gtm-doctor`.

## Emergency and privacy
- **Stop all sending now:** `bin/gtm sending pause`. Resume with `bin/gtm sending resume` (needs your PIN).
- **Someone asks to be removed:** `bin/gtm forget` in your terminal. It deletes them everywhere and blocks them forever.
- Auto-send is **off** by default. Leave it off until you trust the emails.

## Good to know
- Products are kept separate. Add another with `/gtm-onboard`.
- Paid tools (Apify, Snov) only run on the shortlisted best leads, with spend caps.
- Every email claim links back to a source. Every lead links to the post or page that triggered it.
