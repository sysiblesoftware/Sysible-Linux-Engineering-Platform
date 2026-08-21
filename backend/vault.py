"""SLEP secrets vault — encrypt secrets at rest.

Secret values are encrypted with Fernet (AES-128-CBC + HMAC) using a key stored
at data/vault.key (0600), auto-generated on first use. The database only ever
holds ciphertext; plaintext exists only transiently — when an operator creates a
secret, and when a run injects it. Referenced from playbooks as `{{ vault.NAME }}`.

Note (CE): the key sits next to the DB, so this protects a stolen DB/backup, not a
full host compromise. EE can move the key to an external KMS behind this same API.
"""
from __future__ import annotations

import os
from pathlib import Path

_DATA = Path(os.environ.get("SLEP_DATA_DIR", "./data")).resolve()
KEY_FILE = Path(os.environ.get("SLEP_VAULT_KEY", str(_DATA / "vault.key")))


def _key() -> bytes:
    from cryptography.fernet import Fernet
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    k = Fernet.generate_key()
    fd = os.open(str(KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, k)
    finally:
        os.close(fd)
    return k


def encrypt(plaintext: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_key()).encrypt((plaintext or "").encode()).decode()


def decrypt(token: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_key()).decrypt((token or "").encode()).decode()
