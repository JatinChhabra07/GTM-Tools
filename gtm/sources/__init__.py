"""External data sources. Each client is plain Python called by gtm code paths (plan §1.2);
GTM_FAKE=1 replays recorded fixtures instead (gtm/fake.py)."""


def optional(ctx, label: str, fn, *args, **kw):
    """Run one paid collector. A missing or rejected key skips just that collector, with a note in the
    run report, so the free collectors still run (e.g. before the founder has added an Apify key)."""
    from gtm.sources.apify import ApifyUnavailable
    from gtm.sources.snov import SnovUnavailable
    try:
        return fn(*args, **kw)
    except (ApifyUnavailable, SnovUnavailable) as e:
        msg = f"{label} skipped: {e}"
        if msg not in ctx.notes:
            ctx.note(msg)
        return None
