# GTM-Tools: the outreach automation, in plain English

**What it does:** you give it your product's website. Every day it finds 10–15 companies and
people who show a real sign of needing your product, checks their email addresses, and writes a
short personal email to each. **Nothing is sent until you approve it with your PIN.**

## How it works (one day)

1. **Learns your product** from your website and keeps a separate memory per product
   (mem0). Products never mix.
2. **Learns your ideal customer** from you and from your existing clients: "Snapdeal uses us, so
   find companies like Snapdeal."
3. **Watches for buying signals** twice a day:
   - companies hiring for the problem you solve, raising money, changing leaders or tools
     (Apify, wigolo, website checks)
   - named people on LinkedIn/X asking for a solution or complaining about a competitor
   - Reddit / Hacker News (last30days) teach it how buyers describe the pain; those posters are
     never emailed
4. **Finds the right person** at each company (Apify) and **checks the email address** (Snov.io).
5. **Filters hard:** a panel of AI reviewers (the graph-engineering pattern) throws out weak,
   risky or wrong leads. 10 great leads beat 50 random ones.
6. **Writes the emails:** short, human, one specific reason to reach out, no AI-sounding phrases.
7. **You review** in your terminal (`bin/gtm review`) and approve with your PIN.
8. **Snov.io sends** them, follows up twice, and stops the moment someone replies.
9. **Learns from replies:** unsubscribes are blocked forever, and what works is fed back in.

## Getting started (founder)

1. Open Claude Code in this folder and type **`/gtm-setup`**. It asks you everything it needs,
   one question at a time.
2. It will ask you to run two commands **in your own terminal** (never paste keys or your PIN
   into chat):
   - `bin/gtm keys init`: creates your approval PIN
   - `bin/gtm secrets set`: saves your Apify and Snov.io API keys
3. `/gtm-doctor` checks everything is working.

## Safety built in
- No email goes out without your PIN-signed approval (auto-send is possible later, only after a
  proven track record).
- The AI that reads scraped web content has no tools, so a malicious post can't make it do
  anything.
- Unsubscribes are honoured across all products. Spending caps on every run.
- Works on a Claude Max 5x plan (fewer AI calls per run) or Max 20x.

The full technical plan is in `plan.md`; build progress is tracked there too.
