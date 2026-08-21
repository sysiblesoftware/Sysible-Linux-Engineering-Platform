"""Secrets vault — encrypt at rest, never return values, valid-name gate, and the
run-injection data path (ciphertext → plaintext for `vault.<name>`)."""
import backend.db as db
import backend.vault as vault


def test_encrypt_decrypt_roundtrip():
    tok = vault.encrypt("s3cr3t-value")
    assert tok != "s3cr3t-value"                 # not plaintext
    assert vault.decrypt(tok) == "s3cr3t-value"


def test_set_list_hides_value_and_stores_ciphertext(client):
    r = client.post("/vault", json={"name": "db_password", "value": "hunter2"})
    assert r.status_code == 200, r.text
    listed = client.get("/vault").json()["secrets"]
    s = [x for x in listed if x["name"] == "db_password"][0]
    assert "value" not in s                       # value never returned
    # Stored ciphertext decrypts back to the plaintext (the run-injection path).
    cipher = dict(db.all_secret_ciphertexts())["db_password"]
    assert cipher != "hunter2" and vault.decrypt(cipher) == "hunter2"


def test_upsert_replaces_value(client):
    client.post("/vault", json={"name": "token", "value": "one"})
    client.post("/vault", json={"name": "token", "value": "two"})
    names = [x["name"] for x in client.get("/vault").json()["secrets"]]
    assert names.count("token") == 1              # no duplicate
    assert vault.decrypt(dict(db.all_secret_ciphertexts())["token"]) == "two"


def test_invalid_name_rejected(client):
    assert client.post("/vault", json={"name": "1bad name", "value": "x"}).status_code == 400
    assert client.post("/vault", json={"name": "ok", "value": ""}).status_code == 400


def test_delete_secret(client):
    sid = client.post("/vault", json={"name": "temp", "value": "v"}).json()["id"]
    assert client.delete("/vault/" + str(sid)).json()["status"] == "deleted"
    assert "temp" not in [x["name"] for x in client.get("/vault").json()["secrets"]]
