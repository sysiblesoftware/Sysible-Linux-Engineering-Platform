"""Self-signed TLS for the SLEP console — the same approach Sysible Controller
uses: generate a self-signed certificate at first start (scoped to localhost, the
hostname, and any operator-supplied names/IPs) so the console can be served over
HTTPS with no external CA. HTTPS is what makes the browser treat the page as a
"secure context" — which is what restores clipboard/paste in the Monaco IDE and
encrypts the console across the network.

A self-signed cert triggers a one-time browser trust warning; adding the server's
real IP/hostname to SLEP_TLS_HOSTS removes the name-mismatch part of it. For a
warning-free cert, terminate TLS at a reverse proxy with a real (e.g. Let's
Encrypt) certificate instead — see docs/RUNNING.md.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import socket
from pathlib import Path


def _san_entries(hosts):
    """Turn a list of names/IPs into x509 SAN objects (IPs → IPAddress, else DNS)."""
    from cryptography import x509
    seen, sans = set(), []
    for h in hosts:
        h = (h or "").strip()
        if not h or h in seen:
            continue
        seen.add(h)
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            sans.append(x509.DNSName(h))
    return sans


def ensure_cert(cert_dir, extra_hosts=None):
    """Ensure a self-signed cert/key pair exists under cert_dir; create it if not.
    Returns (cert_path, key_path). Idempotent — regenerate by deleting the files."""
    cert_dir = Path(cert_dir)
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "console.crt"
    key_path = cert_dir / "console.key"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    hosts = ["localhost", "127.0.0.1", "::1"]
    try:
        hosts.append(socket.gethostname())
    except OSError:
        pass
    hosts += [h for h in (extra_hosts or []) if h]
    hosts += [h for h in (os.environ.get("SLEP_TLS_HOSTS", "").split(",")) if h.strip()]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Sysible Linux Engineering Platform")])
    # No wall-clock in some sandboxes is fine here (this only runs at real start).
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(_san_entries(hosts)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    # key 0600, cert 0644
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    finally:
        os.close(fd)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


if __name__ == "__main__":  # `python -m backend.selfsigned <dir> [host ...]` — used by the entrypoint
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "./data/tls"
    c, k = ensure_cert(d, sys.argv[2:])
    print(f"{c}\n{k}")
