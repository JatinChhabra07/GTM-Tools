"""One sequence = 3 rendered emails. Rendering and hashing live here so write time, edit time,
review time and push time all compute exactly the same thing (plan §3 content_hash)."""

from __future__ import annotations

import json
import sqlite3

from gtm import validate
from gtm.config import ProductConfig
from gtm.util import display_company, sha256


def steps_from_json(raw: str) -> list[validate.Step]:
    return [validate.Step(int(s["step"]), s.get("subject", ""), s["body"]) for s in json.loads(raw)]


def steps_to_json(steps: list[validate.Step]) -> str:
    return json.dumps([{"step": s.step, "subject": s.subject, "body": s.body} for s in steps],
                      ensure_ascii=False)


def first_name(full_name: str) -> str:
    return (full_name or "").strip().split(" ")[0] if full_name else ""


def context(cfg: ProductConfig, *, seq: sqlite3.Row | dict, lead: sqlite3.Row, account: sqlite3.Row,
            notice: str | None) -> validate.Context:
    frozen = json.loads(seq["context_json"] or "{}")
    return validate.Context(
        recipient_email=seq["recipient_email"],
        db_recipient_email=lead["email"] or "",
        lead_first_name=lead["first_name"] or first_name(lead["full_name"]),
        lead_full_name=lead["full_name"] or "",
        company=display_company(account["name"], account["domain"]),
        is_signal_author=bool(lead["is_signal_author"]),
        reference_policy=seq["reference_policy"],
        allowed_urls=frozen.get("allowed_urls", []),
        proof_quotes=frozen.get("proof_quotes", []),
        claimable_names=frozen.get("claimable_names", []),
        signal_texts=frozen.get("signal_texts", []),
        notice=notice,
        sender_name=cfg.voice.sender_name,
        postal_address=cfg.voice.postal_address,
        opt_out_line=cfg.voice.opt_out_line,
        banned_extra=list(cfg.voice.banned_extra),
    )


def rendered(steps: list[validate.Step], ctx: validate.Context) -> list[dict]:
    return [{"step": s.step, "subject": s.subject, "text": validate.render_step(s, ctx)} for s in steps]


def content_hash(*, seq_id: str, lead_key: str, recipient_email: str, mailbox_id: str,
                 steps: list[validate.Step], ctx: validate.Context) -> str:
    """sha256 over everything the recipient will see + who gets it + which mailbox sends it."""
    parts = [seq_id, lead_key, recipient_email.lower(), mailbox_id, ctx.sender_name]
    for r in rendered(steps, ctx):
        parts += [str(r["step"]), r["subject"], r["text"]]
    return sha256(*parts)


def custom_fields(steps: list[validate.Step], ctx: validate.Context) -> dict[str, str]:
    """What the Snov campaign template reads ({{gtm_subject}}, {{gtm_body_1}}..). Snov custom
    fields use <br> for line breaks; the hash covers the plain text these are made from."""
    out = {}
    for r in rendered(steps, ctx):
        if r["step"] == 1:
            out["gtm_subject"] = r["subject"]
        out[f"gtm_body_{r['step']}"] = r["text"].replace("\r", "").replace("\n", "<br>")
    return out
