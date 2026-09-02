"""Security-hardening regressions (SLEP), mirroring Controller's guards:
login throttle + durable lockout, anti-enumeration decoy, PBKDF2 upgrade-on-login,
durable sessions with revoke-on-demote, password policy, and the tamper-evident
audit chain."""
import hashlib

import backend.db as db


def _raw(client, path, **json):
    # A login/setup call ignores the fixture's Authorization header.
    return client.post(path, json=json)


def test_password_policy_enforced_on_user_create(client):
    # Too short.
    assert client.post("/users", json={"username": "weak1", "password": "short", "role": "operator"}).status_code == 400
    # Long but single character class (all lowercase).
    r = client.post("/users", json={"username": "weak2", "password": "alllowercase", "role": "operator"})
    assert r.status_code == 400
    # Two classes, long enough → ok.
    assert client.post("/users", json={"username": "strong1", "password": "Strong-pw12", "role": "operator"}).status_code == 200


def test_unknown_and_wrong_password_both_401(client):
    assert _raw(client, "/login", username="no-such-user-xyz", password="whatever").status_code == 401
    # Wrong password for the real admin is the same generic 401.
    assert _raw(client, "/login", username="admin", password="wrong-password").status_code == 401


def test_login_throttle_locks_after_repeated_failures(client):
    codes = [_raw(client, "/login", username="throttle-victim", password="bad").status_code for _ in range(12)]
    assert 429 in codes, "repeated failures should eventually be throttled"


def test_login_source_ip_uses_trusted_last_xff_hop():
    # Behind the single SLOP gateway the real client is the RIGHTMOST X-Forwarded-For
    # hop (the one our own proxy appended). A client-injected leftmost entry must be
    # ignored, so an attacker can't rotate the first hop to dodge the IP throttle or
    # forge a victim's IP. Mirrors the SLOP IdP's _client_ip.
    from backend.app import _login_source_ip

    class _Req:
        def __init__(self, xff, peer):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": peer})() if peer else None

    # Caddy appends the true client (10.0.0.9) after a spoofed leftmost value.
    assert _login_source_ip(_Req("1.2.3.4, 10.0.0.9", "172.16.0.1")) == "10.0.0.9"
    # No XFF (standalone) → direct peer.
    assert _login_source_ip(_Req("", "192.168.1.5")) == "192.168.1.5"


def test_pbkdf2_hash_is_iterations_prefixed(client):
    client.post("/users", json={"username": "hashfmt", "password": "Strong-pw12", "role": "operator"})
    row = db.get_admin("hashfmt")
    assert row["pw_hash"].startswith("600000$"), "new hashes carry the iteration count"


def test_legacy_hash_upgrades_on_login(client):
    # Seed a legacy (bare-digest, 200k) hash directly, then log in.
    salt = "deadbeefdeadbeef"
    legacy = hashlib.pbkdf2_hmac("sha256", b"Legacy-pw123", salt.encode(), 200_000).hex()
    db.add_admin("legacyuser", legacy, salt, role="operator")
    assert "$" not in db.get_admin("legacyuser")["pw_hash"]
    r = _raw(client, "/login", username="legacyuser", password="Legacy-pw123")
    assert r.status_code == 200
    assert db.get_admin("legacyuser")["pw_hash"].startswith("600000$"), "verified login upgrades the hash"


def test_sessions_are_durable_and_revoked_on_demote(client):
    client.post("/users", json={"username": "demoteme", "password": "Strong-pw12", "role": "operator"})
    tok = _raw(client, "/login", username="demoteme", password="Strong-pw12").json()["token"]
    # Token works.
    assert client.get("/me", headers={"Authorization": f"Bearer {tok}"}).json()["username"] == "demoteme"
    # Token survives a "restart" (it's in the DB, not memory).
    assert db.resolve_admin_token(tok) is not None
    # Superuser demotes them → their live session is revoked immediately.
    client.patch("/users/demoteme", json={"role": "viewer"})
    assert client.get("/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_audit_chain_records_and_verifies(client):
    # Actions above have written audit rows; the chain must verify.
    v = client.get("/audit/verify").json()
    assert v["ok"] is True and v["entries"] > 0
    entries = client.get("/audit").json()["entries"]
    assert any(e["event"] == "login" for e in entries)


def test_audit_chain_detects_tampering(client):
    db.log_audit("canary", "tester", "before tamper")
    with db._connect() as c:
        row = c.execute("SELECT id FROM admin_audit_log ORDER BY id DESC LIMIT 1").fetchone()
        c.execute("UPDATE admin_audit_log SET detail='forged' WHERE id=?", (row["id"],))
    v = db.verify_audit_chain()
    assert v["ok"] is False and v["broken_at"] == row["id"]
