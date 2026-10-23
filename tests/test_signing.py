import os

import pytest

from gtm import signing

PIN = "correct-horse-42"


def _payload(**over):
    base = dict(product="acme", target="sequence", target_id="seq1", content_hash="h1",
                approved_by="founder", approved_at="2026-10-23T10:00:00Z", via="tty_pin", edited=False)
    base.update(over)
    return signing.approval_payload(**base)


def test_sign_and_verify_roundtrip():
    signing.init_keys(PIN)
    sig = signing.sign(signing.unlock(PIN), _payload())
    assert signing.verify(_payload(), sig)


def test_wrong_pin_is_rejected():
    signing.init_keys(PIN)
    with pytest.raises(signing.WrongPin):
        signing.unlock("wrong-pin-123")


def test_forged_or_missing_signature_never_verifies():
    signing.init_keys(PIN)
    assert not signing.verify(_payload(), None)
    assert not signing.verify(_payload(), "")
    assert not signing.verify(_payload(), "AAAA")
    other = signing.Ed25519PrivateKey.generate()  # an attacker's own key
    assert not signing.verify(_payload(), signing.sign(other, _payload()))


def test_signature_is_bound_to_content_and_product():
    signing.init_keys(PIN)
    sig = signing.sign(signing.unlock(PIN), _payload())
    assert not signing.verify(_payload(content_hash="h2"), sig)      # edited after approval
    assert not signing.verify(_payload(product="other"), sig)        # replayed in another product
    assert not signing.verify(_payload(via="policy"), sig)


def test_weak_pin_refused_and_keys_not_overwritten():
    with pytest.raises(signing.SigningError):
        signing.init_keys("1234")
    signing.init_keys(PIN)
    with pytest.raises(signing.SigningError, match="already exist"):
        signing.init_keys("another-pin-99")


def test_change_pin_needs_old_pin_and_keeps_key():
    signing.init_keys(PIN)
    sig = signing.sign(signing.unlock(PIN), _payload())
    with pytest.raises(signing.WrongPin):
        signing.change_pin("nope-nope-1", "new-pin-5678")
    signing.change_pin(PIN, "new-pin-5678")
    assert signing.verify(_payload(), sig)  # same key, so old approvals stay valid
    signing.unlock("new-pin-5678")


def test_key_files_are_private():
    signing.init_keys(PIN)
    for name in ("approval_key.enc", "approval_key.pub"):
        mode = os.stat(signing.config_dir() / name).st_mode & 0o777
        assert mode == 0o600
    assert PIN.encode() not in (signing.config_dir() / "approval_key.enc").read_bytes()
