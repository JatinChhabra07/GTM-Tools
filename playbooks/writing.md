# Writing playbook

Editable sales knowledge for the email writer. The writer loads one `##` section at a time, so
each section stands on its own. Rules marked "(enforced)" are also checked in code by
`gtm/validate.py`; if you change a number here, change it there too (the playbook lint test fails
when they disagree).

Seeded from plan §5.2 and adapted, with credit, from Harvey's `skills/email_frameworks.md` and
`skills/signal_playbook.md` (MIT, see `NOTICE`). Harvey's contradictions are fixed here: its
60-word follow-ups, its six-touch cadence and its "one exclamation mark per sequence" are gone.

## Core principles

- The order of impact is deliverability, then list relevance, then copy. Copy is the last 20%.
- Write like a real person sending a real email to one person. Read it out loud; if it sounds
  like marketing, rewrite it.
- One observation, one question. That is the whole email.
- A wrong specific is worse than a right generic. Never claim more than the evidence shows.
- Every claim traces to a proof quote or a `facts.yaml` fact (enforced for numbers and client
  names). No invented customers, stats, deadlines or scarcity.
- It's about them. Their situation shows up before our product does; in email 1 our product
  usually isn't named at all.

## Length and shape

Word caps count the body only, not the footer (enforced):

- Email 1: at most 75 words.
- Email 2: at most 75 words.
- Email 3: at most 40 words.

Plain text only (enforced): no HTML, no bold, no bullets, no headers. Short paragraphs of one to
three sentences. Vary sentence length: a four-word line next to a twenty-word one reads human.

## Email 1: the observation

1. One line about something specific and true about them: the primary signal, stated plainly.
2. One line connecting it to the problem it usually creates.
3. One easy-yes question, e.g. "is this on your radar at all?"

Rules (enforced): don't start with "I" or "We"; exactly one question; no exclamation marks;
no link. No pitch and no meeting ask in email 1: the goal is a reply, not a sale.

Good:

```
Sam, Acme is hiring a second support lead. That usually means ticket volume grew faster
than the team did.

Is first-response time something you're watching right now?
```

## Email 2: a new angle

A different angle from email 1, never a repeat of its hook. Don't refer back to email 1. One
specific pain stated plainly, one proof point if there is a real one, then one direct question.
No time words ("recent", "this week"): it is sent days after it was written (enforced).

## Email 3: the breakup

Short (at most 40 words), respectful, no pitch and no guilt trip. Acknowledge the timing may be
wrong, leave the door open, stop. A breakup that lists what they are losing is still a pitch.

Good:

```
Sounds like the timing isn't right, which is fair.

If first-response time ever becomes a priority, I'm easy to find.
```

## Cadence

Three touches. Follow-ups go on day 4 and day 12 after email 1 (`sending.followup_days` in
product.yaml). Snov stops the sequence as soon as the company replies.

## Subject lines

Enforced: 2 to 5 words, lowercase (the company name may keep its own spelling), no lead name, no
fake "Re:" or "Fwd:", and never the overused two-word curiosity subject from the banned list.
It should look like an internal email, not a campaign. Follow-ups may leave the subject empty to
stay in the same thread.

Good: `acme support queue`, `first-response time`, `closing the loop`.

## Choosing the hook

Signal rank for the opener: stated intent, then hiring, then funding, then a tech change, then
tech present, then industry. Use exactly ONE signal. Stacking two or three reads like a dossier.
Keep the others for email 2. See `signals.md` for how to turn each signal into a first line.

## Reference policy

Decided in code from how the signal relates to the recipient (enforced):

- `self_public_professional`: the lead wrote it on LinkedIn or a named X account. Reference the
  topic, not the act of watching. "Your post" is allowed only here.
- `company_event`: reference the implication ("teams scaling a support function usually hit...").
- `theme_only`: a colleague or an anonymous person wrote it. Pain theme only; don't mention where
  it came from.

Never mention Reddit, Instagram or a subreddit. Never quote more than 15 words of a signal
verbatim; paraphrase it.

## Links and tracking

No link in email 1. At most one link across the whole sequence, and only to a URL on the
product's allowlist (enforced). No open or click tracking: tracking pixels cost inbox placement.

## Personalisation that works

Reference their product, their market, or something visible on their own site. Hiring: talk
about the workload behind the hire, not the hire itself. Name at most one tool; never list their
stack back at them. Empty flattery ("you've clearly built something real") is not
personalisation.

## Truthful identity

The from-name, company and footer are the real sender. Never pose as a customer, a mutual
contact or someone with a prior relationship. If a prospect asks whether a message was automated,
answer truthfully (see `replies.md`).

## Banned phrases

One phrase per line. The validator rejects any email containing one (case-insensitive, whole
words); `voice.banned_extra` in product.yaml adds more per product. Don't use these anywhere
else in the playbooks either: the lint test checks.

- quick question
- i hope this finds you well
- hope this email finds you
- hope you're doing well
- hope you are doing well
- just following up
- following up on my last email
- just checking in
- checking in
- circling back
- circle back
- touching base
- touch base
- reaching out
- i wanted to
- just wanted to
- i'd love to
- i would love to
- picture this
- imagine if
- what if i told you
- here's what i keep seeing
- in today's
- worth a quick look
- worth a quick chat
- worth a quick call
- let me know if you're interested
- are you the right person
- no pressure
- no obligation
- congrats on
- congratulations
- game-changer
- game changer
- game-changing
- revolutionize
- revolutionary
- cutting-edge
- cutting edge
- best-in-class
- world-class
- next-level
- supercharge
- unlock your
- seamless
- seamlessly
- synergy
- leverage
- utilize
- facilitate
- robust
- holistic
- paradigm
- delve
- realm
- pivotal
- harness
- transformative
- innovative
- it's worth noting
- generally speaking
- needless to say
- at the end of the day
- to be honest
- in order to
- due to the fact
- act now
- buy now
- click here
- limited time
- special offer
- exclusive offer
- risk-free
- 100% free
- guaranteed
- as an ai
- as a language model
- —
- –
