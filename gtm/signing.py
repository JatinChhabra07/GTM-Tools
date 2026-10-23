"""Approval signatures (plan §0 #4).

The private Ed25519 key is stored encrypted under the founder's PIN (scrypt -> AES-256-GCM) in
~/.config/gtm-tools/approval_key.enc (0600, denied to Claude sessions). Anything can write an
`approvals` row; only someone who knows the PIN can produce a signature that verifies against
approval_key.pub. gtm run verifies before every push, so unsigned or forged rows never send.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from gtm.paths import config_dir, ensure_private_dir

MIN_PIN_LEN = 8
# scrypt cost: ~0.1 s and 32 MiB per guess, which makes offline guessing of a decent PIN slow
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


class SigningError(Exception):
    pass


class WrongPin(SigningError):
    pass


def _key_path() -> Path:
    return config_dir() / "approval_key.enc"


def _pub_path() -> Path:
    return config_dir() / "approval_key.pub"


def keys_exist() -> bool:
    return _key_path().exists() and _pub_path().exists()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode())


def _derive(pin: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(pin.encode())


def _check_pin_strength(pin: str) -> None:
    if len(pin) < MIN_PIN_LEN:
        raise SigningError(f"PIN must be at least {MIN_PIN_LEN} characters")
    if len(set(pin)) < 4:
        raise SigningError("PIN is too repetitive")


def _write_private(path: Path, data: str) -> None:
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, 0o600)


def _store(private: Ed25519PrivateKey, pin: str) -> None:
    raw = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_derive(pin, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)).encrypt(nonce, raw, b"gtm-approval-key")
    blob = {"v": 1, "kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": _b64(salt), "nonce": _b64(nonce), "ct": _b64(ct)}
    _write_private(_key_path(), json.dumps(blob))
    pub = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    _write_private(_pub_path(), _b64(pub))


def init_keys(pin: str) -> None:
    if keys_exist():
        raise SigningError("signing keys already exist; use change_pin (needs the old PIN)")
    _check_pin_strength(pin)
    _store(Ed25519PrivateKey.generate(), pin)


def unlock(pin: str) -> Ed25519PrivateKey:
    if not keys_exist():
        raise SigningError("no signing keys yet: run `bin/gtm keys init` in your own terminal")
    blob = json.loads(_key_path().read_text())
    key = _derive(pin, _unb64(blob["salt"]), blob["n"], blob["r"], blob["p"])
    try:
        raw = AESGCM(key).decrypt(_unb64(blob["nonce"]), _unb64(blob["ct"]), b"gtm-approval-key")
    except InvalidTag:
        raise WrongPin("wrong PIN") from None
    return Ed25519PrivateKey.from_private_bytes(raw)


def change_pin(old_pin: str, new_pin: str) -> None:
    _check_pin_strength(new_pin)
    _store(unlock(old_pin), new_pin)


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign(private: Ed25519PrivateKey, payload: dict) -> str:
    return _b64(private.sign(canonical(payload)))


def verify(payload: dict, signature: str | None) -> bool:
    """True only for a signature made by the founder's key over exactly this payload."""
    if not signature or not _pub_path().exists():
        return False
    pub = Ed25519PublicKey.from_public_bytes(_unb64(_pub_path().read_text().strip()))
    try:
        pub.verify(_unb64(signature), canonical(payload))
    except (InvalidSignature, ValueError):
        return False
    return True


def approval_payload(*, product: str, target: str, target_id: str, content_hash: str,
                     approved_by: str, approved_at: str, via: str, edited: bool) -> dict:
    """What an approval signature covers. `product` stops replaying a signature in another product."""
    return {"kind": "approval", "product": product, "target": target, "target_id": target_id,
            "content_hash": content_hash, "approved_by": approved_by, "approved_at": approved_at,
            "via": via, "edited": bool(edited)}


def autosend_payload(*, product: str, enabled_at: str, approved_by: str) -> dict:
    return {"kind": "autosend", "product": product, "enabled_at": enabled_at, "approved_by": approved_by}


def resume_payload(*, at: str, approved_by: str) -> dict:
    return {"kind": "resume_sending", "at": at, "approved_by": approved_by}


def mailbox_payload(*, mailbox_id: str, address: str, daily_cap: int, warmup_start: str | None,
                    warmup_min_days: int, start_sends: int, daily_increase: int,
                    catchall_allowed: bool, brakes: int = 0) -> dict:
    """A mailbox's sending limits are signed too: raising a cap needs the PIN. `brakes` = bounce brakes
    the founder has acknowledged; a new brake invalidates the signature until they re-sign."""
    return {"kind": "mailbox", "id": mailbox_id, "address": address.lower(), "daily_cap": daily_cap,
            "warmup_start": warmup_start, "warmup_min_days": warmup_min_days, "start_sends": start_sends,
            "daily_increase": daily_increase, "catchall_allowed": bool(catchall_allowed), "brakes": int(brakes)}
