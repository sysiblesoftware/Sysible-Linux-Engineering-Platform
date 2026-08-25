"""SLEP secrets vault — encrypt secrets at rest.

Secret values are encrypted with Fernet (AES-128-CBC + HMAC) using a key stored
at data/vault.key (0600), auto-generated on first use. The database only ever
holds ciphertext; plaintext exists only transiently — when an operator creates a
secret, and when a run injects it. Referenced from playbooks as `{{ vault.NAME }}`.

Key source, in priority order:
  1. SLEP_VAULT_KEY_VALUE — the Fernet key ITSELF (base64), supplied out-of-band
     (env/secret manager). Preferred: the key never lands on disk beside the DB, so a
     stolen data dir / backup yields only ciphertext.
  2. SLEP_VAULT_KEY — a path to a key file (may point outside the data dir / onto
     separate media).
  3. data/vault.key — auto-generated on first use (convenient default; but co-located
     with the DB, so it only protects a DB-file-only leak, not a data-dir/backup theft).
Supplying the key by value (1) is strongly recommended for anything but a single-host
demo.
"""
from __future__ import annotations

import os
from pathlib import Path

_DATA = Path(os.environ.get("SLEP_DATA_DIR", "./data")).resolve()
KEY_FILE = Path(os.environ.get("SLEP_VAULT_KEY", str(_DATA / "vault.key")))


def _key() -> bytes:
    from cryptography.fernet import Fernet
    # 1) Key supplied by value out-of-band — never touches the disk.
    val = os.environ.get("SLEP_VAULT_KEY_VALUE", "").strip()
    if val:
        return val.encode()
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
