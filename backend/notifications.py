"""Notifications — tell someone when a run finishes.

An automation you publish for other people is only useful if its failures reach
somebody. SLEP's runs were visible in the console and nowhere else, so a nightly
schedule that started failing kept failing until a human happened to look.

Two kinds, because they cover different places people actually watch:
  * webhook — a JSON POST, which is how Slack, Teams, PagerDuty, Opsgenie and
    anything homegrown take an alert;
  * email — SMTP, for the people who are not in a chat tool.

Three rules this module exists to hold:

  1. A NOTIFICATION MUST NOT BREAK A RUN. Delivery happens on a background
     thread with a bounded timeout, and every failure lands in the log instead of
     the caller. A run that finished is finished; a chat server being down does
     not get to change that, and must not leave the run row saying "running".

  2. THE PAYLOAD MUST NOT CARRY SECRETS. A run's extra_vars can hold whatever an
     operator typed into the Variables box, and a webhook URL is an address
     outside this platform. The payload is the facts about the run — which
     template, which project, status, exit code, timing, a link — and never its
     variables or its log.

  3. ONLY http(s). A URL is operator-supplied and SLEP posts to it from inside
     the network. file://, gopher:// and friends turn "notify me" into "read
     this host's files", and there is no legitimate use for them here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import smtplib
import threading
import urllib.error
import urllib.request
from email.message import EmailMessage
from urllib.parse import urlsplit

KINDS = ("webhook", "email")
EVENTS = ("success", "failure")
TIMEOUT = 10.0
MAX_BODY = 64 * 1024


class NotificationError(ValueError):
    pass


# ---------------------------------------------------------------------------
# configuration, checked when it is saved
# ---------------------------------------------------------------------------
def validate_config(kind: str, cfg: dict) -> dict:
    if kind not in KINDS:
        raise NotificationError(f"unknown notification kind '{kind}'")
    cfg = dict(cfg or {})

    if kind == "webhook":
        url = str(cfg.get("url") or "").strip()
        if not url:
            raise NotificationError("a webhook needs a URL")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise NotificationError(
                f"a webhook URL must be http or https, not '{parts.scheme or url}' — "
                f"SLEP posts to it from inside your network")
        if not parts.netloc:
            raise NotificationError("that webhook URL has no host")
        return {"url": url, "secret": str(cfg.get("secret") or "")}

    host = str(cfg.get("host") or "").strip()
    if not host:
        raise NotificationError("email needs an SMTP host")
    to = cfg.get("to") or []
    if isinstance(to, str):
        to = [a.strip() for a in to.replace(";", ",").split(",") if a.strip()]
    if not to:
        raise NotificationError("email needs at least one recipient")
    for addr in to:
        if "@" not in addr or addr.startswith("@") or addr.endswith("@"):
            raise NotificationError(f"'{addr}' is not an email address")
    try:
        port = int(cfg.get("port") or 587)
    except (TypeError, ValueError):
        raise NotificationError("the SMTP port must be a number")
    if not 1 <= port <= 65535:
        raise NotificationError("the SMTP port must be 1-65535")
    return {"host": host, "port": port, "to": to,
            "from": str(cfg.get("from") or "slep@sysible.local").strip(),
            "username": str(cfg.get("username") or ""),
            "password": str(cfg.get("password") or ""),
            "starttls": bool(cfg.get("starttls", True))}


def wants(rule: dict, status: str) -> bool:
    """Does this rule fire for a run that ended in `status`?"""
    if status == "success":
        return bool(rule.get("on_success"))
    if status in ("failed", "error", "canceled"):
        return bool(rule.get("on_failure"))
    return False


# ---------------------------------------------------------------------------
# the message
# ---------------------------------------------------------------------------
def payload(run: dict, project=None, template=None, base_url="") -> dict:
    """The facts about a finished run — and nothing else.

    Deliberately NOT extra_vars and NOT the log: both can hold whatever someone
    typed into the Variables box, and this leaves the platform.
    """
    status = run.get("status") or ""
    name = (template or {}).get("name") or run.get("target") or ""
    body = {
        "event": "run_finished",
        "status": status,
        "ok": status == "success",
        "run_id": run.get("id"),
        "kind": run.get("kind"),
        "target": run.get("target"),
        "exit_code": run.get("exit_code"),
        "started": run.get("started"),
        "finished": run.get("finished"),
        "created_by": run.get("created_by"),
        "project": (project or {}).get("name"),
        "template": (template or {}).get("name"),
        "text": f"SLEP: {name} {'succeeded' if status == 'success' else status}"
                f" (run #{run.get('id')})",
    }
    if base_url:
        body["url"] = f"{base_url.rstrip('/')}/#/runs/{run.get('id')}"
    return body


def _sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------
def deliver(rule: dict, body: dict) -> tuple[bool, str]:
    """Send one notification. Returns (ok, detail). Never raises."""
    try:
        kind = rule.get("kind")
        cfg = rule.get("config") or {}
        if kind == "webhook":
            return _deliver_webhook(cfg, body)
        if kind == "email":
            return _deliver_email(cfg, body)
        return False, f"unknown notification kind '{kind}'"
    except Exception as e:  # noqa: BLE001 — a notifier must not raise at its caller
        return False, f"{type(e).__name__}: {e}"


def _deliver_webhook(cfg: dict, body: dict) -> tuple[bool, str]:
    url = cfg.get("url") or ""
    if urlsplit(url).scheme not in ("http", "https"):
        # Re-checked at SEND time, not only at save time: a stored row can be
        # edited by anything that can write the database.
        return False, "refusing a non-http(s) webhook URL"
    raw = json.dumps(body).encode()[:MAX_BODY]
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Sysible-SLEP")
    if cfg.get("secret"):
        # So the receiver can tell a real notification from anyone who learned
        # the URL — which is the only thing protecting an incoming webhook.
        req.add_header("X-Sysible-Signature", _sign(cfg["secret"], raw))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _deliver_email(cfg: dict, body: dict) -> tuple[bool, str]:
    msg = EmailMessage()
    msg["Subject"] = body.get("text") or "SLEP run finished"
    msg["From"] = cfg.get("from") or "slep@sysible.local"
    msg["To"] = ", ".join(cfg.get("to") or [])
    lines = [f"{k}: {v}" for k, v in body.items() if v not in (None, "") and k != "text"]
    msg.set_content((body.get("text") or "") + "\n\n" + "\n".join(lines) + "\n")
    with smtplib.SMTP(cfg["host"], int(cfg.get("port") or 587), timeout=TIMEOUT) as s:
        if cfg.get("starttls"):
            s.starttls()
        if cfg.get("username"):
            s.login(cfg["username"], cfg.get("password") or "")
        s.send_message(msg)
    return True, "sent"


def send_async(rules, body, on_done=None) -> threading.Thread:
    """Deliver on a background thread. A run is finished the moment its status is
    written; a chat server being down does not get to hold that up, or fail it."""
    def _work():
        for rule in rules or []:
            ok, detail = deliver(rule, body)
            if on_done:
                try:
                    on_done(rule, ok, detail)
                except Exception:  # noqa: BLE001
                    pass
    t = threading.Thread(target=_work, name="slep-notify", daemon=True)
    t.start()
    return t
