"""Email validator: one clean sequence that passes, then one test per rule (plan §5 Phase 7)."""

from dataclasses import replace

import pytest

from gtm import validate
from gtm.validate import (OPT_OUT_KEYWORDS, WORD_CAPS, Context, Step, is_opt_out_phrase,
                          load_banned_phrases, render_footer, render_step, validate_sequence)

SIGNAL = ("We are hiring a Support Team Lead to own our growing ticket queue and build out triage "
          "processes across email chat and phone for our customers worldwide")
LINK = "https://helpdesk-product.example/case-study"


def ctx(**kw) -> Context:
    base = Context(
        recipient_email="sam@acme.example",
        db_recipient_email="Sam@Acme.example",
        lead_first_name="Sam",
        lead_full_name="Sam Rivera",
        company="Acme",
        is_signal_author=False,
        reference_policy="company_event",
        allowed_urls=[LINK],
        proof_quotes=["Helpdesk Co cut first-response time by 40% in one quarter."],
        claimable_names=["Helpdesk Co", "Globex"],
        signal_texts=[SIGNAL],
        notice=None,
        sender_name="Jatin Chhabra",
        postal_address="12 Example Street, Pune 411001",
        opt_out_line='Not interested? Reply "no thanks" and I won\'t email again.',
        banned_extra=[],
    )
    return replace(base, **kw)


def clean_steps() -> list[Step]:
    return [
        Step(1, "acme support queue",
             "Sam, Acme is hiring a second support lead. That usually means ticket volume grew "
             "faster than the team did.\n\nIs first-response time something you're watching right now?"),
        Step(2, "",
             "Teams that add a support lead often find triage eats the first month. Helpdesk Co cut "
             f"first-response time by 40% after moving triage to rules: {LINK}\n\n"
             "Would a short teardown of your queue be useful?"),
        Step(3, "",
             "Sounds like the timing isn't right, which is fair.\n\n"
             "If first-response time ever becomes a priority, I'm easy to find."),
    ]


def with_step(n: int, **kw) -> list[Step]:
    steps = clean_steps()
    steps[n - 1] = replace(steps[n - 1], **kw)
    return steps


def rules(steps=None, c=None) -> set[str]:
    return {v.rule for v in validate_sequence(steps or clean_steps(), c or ctx())}


def assert_only(rule, steps=None, c=None):
    found = validate_sequence(steps or clean_steps(), c or ctx())
    assert {v.rule for v in found} == {rule}, [(v.rule, v.step, v.message) for v in found]
    assert all(v.message for v in found)
    return found


# ---------------------------------------------------------------- the clean case


def test_clean_sequence_passes_everything():
    assert validate_sequence(clean_steps(), ctx()) == []


def test_render_step_is_body_then_footer():
    c = ctx(notice="We found your details on your company site; reply stop and we'll delete them.")
    footer = render_footer(c)
    assert footer.splitlines() == [c.sender_name, c.postal_address, c.notice, c.opt_out_line]
    sent = render_step(clean_steps()[0], c)
    assert sent == clean_steps()[0].body + "\n\n" + footer


def test_render_footer_skips_absent_notice():
    assert render_footer(ctx()).splitlines() == [
        "Jatin Chhabra", "12 Example Street, Pune 411001", ctx().opt_out_line]


# ---------------------------------------------------------------- length


def test_word_cap_email1_80_words():
    body = clean_steps()[0].body.replace("Is first", " ".join(["really"] * 60) + " Is first")
    found = assert_only("word_cap", with_step(1, body=body))
    assert found[0].step == 1 and "75" in found[0].message


def test_word_cap_email3_is_40():
    assert WORD_CAPS == {1: 75, 2: 75, 3: 40}
    assert_only("word_cap", with_step(3, body="Fair enough. " + " ".join(["okay"] * 40)))


def test_word_cap_ignores_footer():
    # 75 body words exactly: the footer's words must not push it over.
    body = "Sam, " + " ".join(["queue"] * 72) + " right now?"
    assert len(validate.words(body)) == 75
    assert "word_cap" not in rules(with_step(1, body=body))


# ---------------------------------------------------------------- subject


@pytest.mark.parametrize("subject", ["support", "a b c d e f"])
def test_subject_word_count(subject):
    assert_only("subject_words", with_step(1, subject=subject))


def test_subject_required_on_step1_but_optional_after():
    assert "subject_words" in rules(with_step(1, subject=""))
    assert "subject_words" not in rules(with_step(2, subject=""))


def test_subject_lowercase_but_company_spelling_allowed():
    assert_only("subject_case", with_step(1, subject="Support queue"))
    assert rules(with_step(1, subject="Acme support queue")) == set()


def test_subject_no_lead_name():
    assert_only("subject_name", with_step(1, subject="sam support queue"))


def test_subject_no_quick_question():
    assert "subject_quick_question" in rules(with_step(1, subject="quick question"))


@pytest.mark.parametrize("subject", ["re: support queue", "Fwd: support queue", "fw: support"])
def test_subject_no_fake_reply(subject):
    assert "subject_fake_reply" in rules(with_step(1, subject=subject))


# ---------------------------------------------------------------- email 1 shape


@pytest.mark.parametrize("opening", ["I noticed Acme", "We noticed Acme", "Hi Sam, I noticed Acme",
                                     "Sam, we noticed Acme", "I'm seeing Acme"])
def test_email1_does_not_start_with_i_or_we(opening):
    body = clean_steps()[0].body.replace("Sam, Acme", opening, 1)
    assert_only("email1_opener", with_step(1, body=body))


def test_opener_rule_only_on_email1():
    assert "email1_opener" not in rules(with_step(3, body="I get it, the timing is off. I'm easy to find."))


def test_no_exclamation_anywhere():
    assert_only("exclamation", with_step(3, body="Sounds like the timing isn't right. I'm easy to find!"))
    assert_only("exclamation", with_step(1, subject="acme support queue!"))


def test_email1_exactly_one_question():
    two = clean_steps()[0].body + " Or is it handled?"
    assert_only("email1_questions", with_step(1, body=two))
    none = clean_steps()[0].body.replace("?", ".")
    assert_only("email1_questions", with_step(1, body=none))


def test_no_link_in_email1():
    steps = clean_steps()
    steps[0] = replace(steps[0], body=steps[0].body.replace("right now?", f"right now? {LINK}"))
    steps[1] = replace(steps[1], body=steps[1].body.replace(f": {LINK}", "."))
    assert_only("email1_link", steps)


def test_bare_domain_counts_as_a_link():
    body = clean_steps()[0].body + " See acme-helpdesk.com"
    assert "email1_link" in rules(with_step(1, body=body))


def test_at_most_one_link_per_sequence():
    steps = with_step(3, body=f"Sounds like the timing isn't right. The case study is here: {LINK}")
    assert_only("link_count", steps)


def test_no_html():
    body = clean_steps()[1].body.replace("Helpdesk Co", "<b>Helpdesk Co</b>")
    assert_only("html", with_step(2, body=body))


# ---------------------------------------------------------------- merge tags & banned phrases


@pytest.mark.parametrize("tag", ["{{first_name}}", "{first_name}", "[FIRST_NAME]", "%FIRSTNAME%",
                                 "{{ company }}", "[company]"])
def test_unrendered_merge_tags(tag):
    body = clean_steps()[0].body.replace("Sam,", f"{tag},")
    assert_only("merge_tag", with_step(1, body=body))


@pytest.mark.parametrize("phrase", ["reaching out", "Reaching Out", "I’d love to", "game-changer",
                                    "seamlessly", "—"])
def test_banned_phrases_from_playbook(phrase):
    body = clean_steps()[2].body.replace("which is fair", f"which is fair {phrase} here")
    assert "banned_phrase" in rules(with_step(3, body=body))


def test_banned_phrase_whole_words_only():
    # "realm" is banned, but "realms" is a different word: no substring false positives.
    body = clean_steps()[2].body.replace("which is fair", "the realms of support differ")
    assert "banned_phrase" not in rules(with_step(3, body=body))


def test_banned_extra_from_voice_config():
    body = clean_steps()[2].body.replace("which is fair", "which is totally fair")
    assert "banned_phrase" not in rules(with_step(3, body=body))
    assert_only("banned_phrase", with_step(3, body=body), ctx(banned_extra=["totally"]))


def test_load_banned_phrases_reads_the_section(tmp_path):
    (tmp_path / "writing.md").write_text(
        "# W\n\n## Intro\n- not a phrase\n\n## Banned phrases\n\nSome prose.\n\n- Foo Bar\n- `baz`\n\n"
        "## After\n- also not\n")
    assert load_banned_phrases(tmp_path) == ["foo bar", "baz"]


def test_load_banned_phrases_refuses_an_empty_list(tmp_path):
    (tmp_path / "writing.md").write_text("# W\n\n## Banned phrases\n\nnothing here\n")
    with pytest.raises(ValueError):
        load_banned_phrases(tmp_path)


def test_real_playbook_banned_list_is_loaded():
    phrases = load_banned_phrases()
    assert "reaching out" in phrases and "quick question" in phrases and "—" in phrases


# ---------------------------------------------------------------- reference policy


@pytest.mark.parametrize("mention", ["on Reddit", "in r/support", "a subreddit", "on Instagram"])
def test_no_platform_mentions(mention):
    body = clean_steps()[1].body.replace("often find", f"{mention} often find")
    assert_only("platform_mention", with_step(2, body=body))


def test_your_post_only_when_lead_is_signal_author():
    body = clean_steps()[0].body.replace("Sam, Acme is hiring", "Sam, your post says Acme is hiring")
    assert_only("your_post", with_step(1, body=body))
    ok = ctx(is_signal_author=True, reference_policy="self_public_professional")
    assert rules(with_step(1, body=body), ok) == set()


def test_theme_only_mentions_no_source():
    body = clean_steps()[0].body.replace("Sam, Acme is hiring", "Sam, saw on LinkedIn that Acme is hiring")
    assert_only("source_mention", with_step(1, body=body), ctx(reference_policy="theme_only"))
    assert "source_mention" not in rules(with_step(1, body=body))


@pytest.mark.parametrize("word", ["recently", "this week", "just saw", "yesterday"])
def test_no_time_words_in_steps_2_and_3(word):
    body = clean_steps()[2].body.replace("Sounds like", f"Sounds like, {word}, ")
    found = assert_only("time_word", with_step(3, body=body))
    assert found[0].step == 3
    early = clean_steps()[0].body.replace("Sam, Acme is hiring", f"Sam, Acme is {word} hiring")
    assert "time_word" not in rules(with_step(1, body=early))


def test_unknown_reference_policy():
    assert_only("reference_policy", c=ctx(reference_policy="whatever"))


# ---------------------------------------------------------------- claims, urls, quotes


@pytest.mark.parametrize("claim", ["teams see 3x faster replies", "it saves $2M a year",
                                   "cut churn by 30%", "with 1,200 tickets a week"])
def test_invented_number_claim(claim):
    body = clean_steps()[1].body.replace("often find", f"often find {claim} and")
    found = assert_only("claim_unproven", with_step(2, body=body))
    assert found[0].step == 2


def test_number_in_proof_is_fine_and_lead_company_exempt():
    c = ctx(company="37signals")
    body = clean_steps()[0].body.replace("Sam, Acme", "Sam, 37signals")
    assert rules(with_step(1, body=body), c) == set()


def test_unproven_client_name():
    body = clean_steps()[1].body.replace("Helpdesk Co cut", "Globex and Helpdesk Co cut")
    found = assert_only("claim_unproven", with_step(2, body=body))
    assert "Globex" in found[0].message


def test_url_outside_allowlist():
    body = clean_steps()[1].body.replace(LINK, "https://random-blog.example/post")
    assert_only("url_not_allowed", with_step(2, body=body))


def test_allowlist_ignores_scheme_and_trailing_slash():
    body = clean_steps()[1].body.replace(LINK, LINK.replace("https://", "http://") + "/")
    assert rules(with_step(2, body=body)) == set()


def test_signal_quote_over_15_words():
    sixteen = " ".join(SIGNAL.split()[2:18])
    body = clean_steps()[1].body.replace("Teams that add", f'"{sixteen}" Teams that add')
    assert_only("long_quote", with_step(2, body=body))
    fifteen = " ".join(SIGNAL.split()[2:17])
    ok = clean_steps()[1].body.replace("Teams that add", f'"{fifteen}" Teams that add')
    assert "long_quote" not in rules(with_step(2, body=ok))


# ---------------------------------------------------------------- recipient & footer


def test_recipient_must_match_db_case_insensitive():
    assert rules(c=ctx(db_recipient_email="SAM@ACME.EXAMPLE")) == set()
    assert_only("recipient_mismatch", c=ctx(db_recipient_email="other@acme.example"))
    assert_only("recipient_mismatch", c=ctx(recipient_email="", db_recipient_email=""))


@pytest.mark.parametrize("field", ["sender_name", "postal_address"])
def test_missing_sender_or_postal_address_holds_the_send(field):
    found = assert_only("footer_missing", c=ctx(**{field: "  "}))
    assert found[0].step is None


def test_missing_opt_out_line():
    assert_only("footer_missing", c=ctx(opt_out_line=""))


@pytest.mark.parametrize("line", ['Reply "stop" or email optout@acme.example',
                                  'Reply "unsubscribe" or visit https://acme.example/unsub',
                                  'Reply "stop" or click to opt out'])
def test_opt_out_line_has_no_link_or_at(line):
    assert_only("opt_out_link", c=ctx(opt_out_line=line))


@pytest.mark.parametrize("line", ["Let me know if this isn't relevant.", "Not for you? Just ignore this."])
def test_opt_out_line_needs_a_recognised_reply_word(line):
    assert_only("opt_out_keyword", c=ctx(opt_out_line=line))


def test_opt_out_keywords():
    assert {"no thanks", "stop", "unsubscribe"} <= set(OPT_OUT_KEYWORDS)
    assert is_opt_out_phrase("STOP")
    assert is_opt_out_phrase("No thanks, we're good")
    assert is_opt_out_phrase("please remove me")
    assert not is_opt_out_phrase("Sounds great, tell me more")
    assert not is_opt_out_phrase("we're unstoppable")  # whole words only


def test_notice_required_by_geo_must_be_present():
    assert_only("notice_missing", c=ctx(notice=""))
    c = ctx(notice="We found your details on your company site; reply stop and we'll delete them.")
    assert rules(c=c) == set()
    assert c.notice in render_step(clean_steps()[0], c)


# ---------------------------------------------------------------- shape


@pytest.mark.parametrize("numbers", [[1, 3], [2], [1, 2, 3, 4], []])
def test_sequence_shape(numbers):
    base = {s.step: s for s in clean_steps()}
    steps = [base.get(n, Step(n, "", "Fair enough.")) for n in numbers]
    assert "sequence_shape" in {v.rule for v in validate_sequence(steps, ctx())}


def test_empty_body():
    assert "empty_body" in rules(with_step(3, body="   "))


def test_exit_criteria_bundle_all_rejected():
    """Plan §5 Phase 7 exit criteria, all at once: every one must be reported."""
    steps = [
        Step(1, "acme support queue",
             "Hi [FIRST_NAME], teams on Reddit see 3x faster replies! " + " ".join(["more"] * 70)
             + f" Is this useful? Or not? {LINK}"),
        Step(2, "", "Teams like yours: https://random-blog.example/x Is it useful?"),
        Step(3, "", "Recently " + " ".join(SIGNAL.split()[:17])),
    ]
    found = {v.rule for v in validate_sequence(steps, ctx(notice=""))}
    assert {"claim_unproven", "platform_mention", "url_not_allowed", "long_quote", "word_cap",
            "exclamation", "email1_questions", "email1_link", "notice_missing", "time_word",
            "merge_tag"} <= found
