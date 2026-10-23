# Replies playbook

How replies are handled (plan §5 Phase 7 safety part, Phase 9 drafts). Adapted, with credit,
from Harvey's `skills/objection_handling.md` and `skills/sales_methodology.md` (MIT, see
`NOTICE`). Harvey's canned objection answers with invented customers and stats are dropped: we
never fabricate a proof point.

## Order of handling

1. Strip our own sent text and footer from the reply first, so a quoted footer is never read as
   the prospect's words.
2. Keyword opt-out check (below). It overrides any model judgement.
3. Keyword escalation: legal, angry or contract wording goes to `needs_human`.
4. Only then the isolated classifier: interested, not now, referral, objection, out of office,
   bounce. A classifier failure goes to `needs_human`.

Any reply at all marks the sequence `replied`, so no further steps go out.

## Opt-outs

Any opt-out wording counts, in any language register: "stop", "unsubscribe", "remove me",
"no thanks", "not interested", "take me off", "don't email me". The keyword list lives in code as
`OPT_OUT_KEYWORDS` in `gtm/validate.py`, and the footer's opt-out line must ask for one of them.

An opt-out is not an objection. Never answer it with a pitch, a question or a last offer. The
address is suppressed globally (every product), the prospect is stopped in every Snov campaign,
and at most one polite line confirms it.

## Diagnose the objection

The stated objection is rarely the real one. Classify before drafting:

- Reflex: a one-line no sent within minutes. They didn't evaluate anything. Close gracefully.
- Condition: "under contract until March". That's a fact, not an objection. Ask permission to
  come back at that date and actually stop until then.
- Smokescreen: "too expensive" from someone who never asked the price. The real issue is
  usually priority or trust; answer that one.
- Genuine: specific and engaged. The only kind that earns a substantive answer.

## Drafting a reply

- Keep the reply shorter than theirs.
- One attempt per objection. If they push back again, respect it.
- Never argue. Match their tone and formality.
- Every claim traces to a proof or a fact, exactly like outbound email.
- If it isn't a fit, say so. Credibility beats one deal.
- Drafts are reviewed and approved in `gtm review` like sequences; nothing is sent unapproved.

## Referrals

"Talk to Priya, she owns this" creates a new `referral=true` lead for the named person only.
Mention who pointed you to them, truthfully, and nothing they didn't say.

## Automated disclosure

If a prospect asks whether they're talking to a bot, AI or automation, the reply says so
truthfully and immediately. The validator never blocks truthful disclosure in replies. Some
jurisdictions (e.g. California's bot disclosure law) require it; everywhere, a lie ends the
conversation.

## Anger, legal and spam complaints

Hostile replies get no rebuttal. "This is spam", "how did you get my email" or "I'm reporting
this" gets one factual, apologetic note (where the data came from, and that it is deleted),
suppression, and a flag for the founder. Contract or legal questions go to `needs_human`.
