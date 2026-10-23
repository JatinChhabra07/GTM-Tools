# Compliance playbook

The rules every email and reply follows. Plan §6 is the source of truth and `docs/compliance.md`
gets legal review before the first real send. This is not legal advice.

## Footer

Every email carries, in this order: the sender's real name, a postal address the sender
receives mail at (street address, PO box or virtual mailbox), the geo notice when required, and
the opt-out line. A missing sender name or postal address holds the send; it is never just a
warning (enforced).

## Opt-out line

The line asks for a plain reply the handler recognises, for example:
`Not interested? Reply "no thanks" and I won't email again.` It may not contain a link or an
email address, because opting out must be as simple as replying (enforced). Opt-outs are
honoured immediately and everywhere.

## Honest headers

Truthful subject lines (no fake "Re:"/"Fwd:"), a real from-identity, no impersonation of a
customer, colleague or mutual contact.

## Per-country policy

Set in `product.yaml` `geo_policy`; unknown countries are blocked.

- US (CAN-SPAM): accurate headers, honest subject, postal address, opt-out honoured at once.
- EU/UK: legitimate interest with a written assessment and an Art. 14 notice; blocked until the
  assessment exists. Germany needs prior consent even for B2B email.
- India (DPDP Act 2023 and Rules 2025): notice, data-source statement and easy withdrawal, and
  only after `legal_signoff` is recorded.

When a country requires a notice, the notice text must be present in the footer (enforced).

## Tracking and links

No open or click tracking. No link in email 1, at most one link in a sequence, and only to an
allowlisted URL.

## Automation disclosure

Answer truthfully when asked whether a message is automated. Never claim to be a human when that
is false.

## Erasure and suppression

`gtm forget <email>` erases a person across every product and store and adds them to global
suppression. Suppression is global: an opt-out from one product's email stops every product.
