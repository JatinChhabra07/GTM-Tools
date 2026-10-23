"""Playbook lint (plan §5 Phase 7): no banned phrase in our own examples, no conflicting numbers.

Harvey's skill files contradicted each other (75 vs 60 words, 3 vs 6 touches) and its examples
used phrases its own gate banned. The writer loads these files as instructions, so a conflict
here becomes a coin flip in the emails. Numbers are also checked against the code that enforces
them (validate.WORD_CAPS, catalog freshness, Sending.followup_days).
"""

import re
from collections import defaultdict
from pathlib import Path

import pytest

from gtm.catalog import CATALOG
from gtm.config import Sending
from gtm.validate import WORD_CAPS, find_phrases, load_banned_phrases

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOKS = ROOT / "playbooks"
NAMES = ("writing", "signals", "replies", "accounts", "compliance")

WORD_CAP_RE = re.compile(
    r"(?:email|step)\s*([1-3])\b[^.\n]{0,40}?(?:at most|≤|<=|under|up to|no more than|max(?:imum)?)"
    r"\s*(\d+)\s*words", re.IGNORECASE)
FOLLOWUP_RE = re.compile(r"follow-?ups?[^.\n]*?\bdays?\s+(\d+)\s+and\s+(?:day\s+)?(\d+)", re.IGNORECASE)
CODE_RE = re.compile(r"`([A-Z][A-Z0-9_]{3,})`")
DAYS_RE = re.compile(r"\b(\d+)\s+days\b")


def texts() -> dict[str, str]:
    return {n: (PLAYBOOKS / f"{n}.md").read_text(encoding="utf-8") for n in NAMES}


def without_banned_section(text: str) -> str:
    return re.sub(r"(?ms)^## Banned phrases\s*$.*?(?=^## |\Z)", "", text)


def word_caps(text: str) -> dict[int, set[int]]:
    found: dict[int, set[int]] = defaultdict(set)
    for step, n in WORD_CAP_RE.findall(text):
        found[int(step)].add(int(n))
    return found


def followups(text: str) -> set[tuple[int, int]]:
    return {(int(a), int(b)) for a, b in FOLLOWUP_RE.findall(text)}


def freshness(text: str) -> dict[str, set[int]]:
    """`CODE` ... N days on one line. A line with several day counts is ambiguous: skipped."""
    found: dict[str, set[int]] = defaultdict(set)
    for line in text.splitlines():
        codes, days = CODE_RE.findall(line), DAYS_RE.findall(line)
        if codes and len(days) == 1:
            for code in codes:
                found[code].add(int(days[0]))
    return found


def merged(per_file: dict[str, dict]) -> dict:
    out: dict = defaultdict(lambda: defaultdict(set))
    for name, found in per_file.items():
        for key, values in found.items():
            for v in values:
                out[key][v].add(name)
    return out


def conflicts(per_file: dict[str, dict]) -> list[str]:
    return [f"{key}: " + ", ".join(f"{v} in {sorted(files)}" for v, files in sorted(vals.items()))
            for key, vals in merged(per_file).items() if len(vals) > 1]


# ---------------------------------------------------------------- structure


@pytest.mark.parametrize("name", NAMES)
def test_playbook_exists_with_sections(name):
    text = (PLAYBOOKS / f"{name}.md").read_text(encoding="utf-8")
    assert len(re.findall(r"(?m)^## \S", text)) >= 3, "the writer loads one ## section at a time"


# ---------------------------------------------------------------- banned phrases


@pytest.mark.parametrize("name", NAMES)
def test_no_banned_phrase_in_playbook_text_or_examples(name):
    banned = load_banned_phrases(PLAYBOOKS)
    hits = find_phrases(without_banned_section(texts()[name]), banned)
    assert hits == [], f"{name}.md uses banned phrase(s) {hits}: rephrase the example"


def test_lint_catches_a_banned_example():
    banned = load_banned_phrases(PLAYBOOKS)
    sample = "## Good\n\n```\nJust wanted to say we're reaching out about Acme.\n```\n"
    assert set(find_phrases(sample, banned)) >= {"just wanted to", "reaching out"}


# ---------------------------------------------------------------- numbers


def test_word_caps_agree_across_playbooks_and_code():
    per_file = {n: word_caps(t) for n, t in texts().items()}
    assert conflicts(per_file) == []
    found = merged(per_file)
    assert set(found) == set(WORD_CAPS), "writing.md must state the cap for every step"
    for step, cap in WORD_CAPS.items():
        assert set(found[step]) == {cap}, f"email {step}: playbooks say {set(found[step])}, code {cap}"


def test_followup_days_agree_across_playbooks_and_config():
    per_file = {n: {"followup_days": followups(t)} for n, t in texts().items()}
    assert conflicts(per_file) == []
    stated = set().union(*(f["followup_days"] for f in per_file.values()))
    assert stated == {tuple(Sending().followup_days)}


def test_signal_freshness_agrees_across_playbooks_and_catalog():
    per_file = {n: freshness(t) for n, t in texts().items()}
    assert conflicts(per_file) == []
    found = merged(per_file)
    for code in CATALOG:
        if code.freshness_days is None:
            continue
        assert code.code in found, f"signals.md has no freshness for {code.code}"
        assert set(found[code.code]) == {code.freshness_days}, code.code


def test_lint_catches_conflicting_numbers():
    a = "Email 1: at most 75 words. Follow-ups go on day 4 and day 12.\n| `FUNDING` | 60 days |\n"
    b = "Keep email 1 under 60 words. Follow-ups on day 3 and 7.\n`FUNDING` is fresh for 90 days.\n"
    assert conflicts({"a": word_caps(a), "b": word_caps(b)})
    assert conflicts({"a": {"f": followups(a)}, "b": {"f": followups(b)}})
    assert conflicts({"a": freshness(a), "b": freshness(b)})
