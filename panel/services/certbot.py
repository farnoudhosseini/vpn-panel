"""TLS certificates for the panel and for decoy sites.

Domains are validated before they reach the command line, and ``certbot`` is
invoked without a shell, so a hostile domain value cannot become an argument
injection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..core.logging import get_logger
from . import paths, shell

log = get_logger("certbot")

LETSENCRYPT_ROOT = Path("/etc/letsencrypt/live")
WEBROOT = paths.fake_site_root() / "decoy"

# A conservative hostname pattern: labels of letters/digits/hyphen, at least
# two labels, no leading or trailing hyphen.
_DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{3,255}$")


class CertificateError(RuntimeError):
    pass


@dataclass(slots=True)
class Certificate:
    domain: str
    certificate: Path
    private_key: Path
    chain: Path | None = None

    def exists(self) -> bool:
        return self.certificate.is_file() and self.private_key.is_file()

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "domain": self.domain,
            "certificate": str(self.certificate),
            "private_key": str(self.private_key),
            "exists": self.exists(),
        }


def validate_domain(domain: str) -> str:
    candidate = (domain or "").strip().rstrip(".")
    if not _DOMAIN_RE.match(candidate):
        raise CertificateError(f"invalid domain {domain!r}")
    return candidate.lower()


def validate_email(email: str) -> str:
    candidate = (email or "").strip()
    if not _EMAIL_RE.match(candidate):
        raise CertificateError(f"invalid email {email!r}")
    return candidate


def certificate_for(domain: str) -> Certificate:
    safe = validate_domain(domain)
    base = LETSENCRYPT_ROOT / safe
    return Certificate(
        domain=safe,
        certificate=base / "fullchain.pem",
        private_key=base / "privkey.pem",
        chain=base / "chain.pem",
    )


def available() -> bool:
    return shell.available("certbot")


def issue(domain: str, email: str, *, staging: bool = False, webroot: Path | None = None) -> dict[str, object]:
    """Obtain a certificate using the HTTP-01 challenge served by nginx."""
    if not available():
        raise CertificateError("certbot is not installed")

    safe_domain = validate_domain(domain)
    safe_email = validate_email(email)
    root = webroot or WEBROOT
    paths.ensure(root, 0o755)

    args = [
        "certbot",
        "certonly",
        "--webroot",
        "-w",
        str(root),
        "-d",
        safe_domain,
        "--non-interactive",
        "--agree-tos",
        "--keep-until-expiring",
        "--rsa-key-size",
        "4096",
        "-m",
        safe_email,
    ]
    if staging:
        args.append("--staging")

    result = shell.run(args, timeout=300)
    certificate = certificate_for(safe_domain)
    outcome = {
        "ok": result.ok and certificate.exists(),
        "domain": safe_domain,
        "message": result.message,
        "certificate": certificate.as_dict(),
    }
    if not outcome["ok"]:
        log.error("certbot failed for %s: %s", safe_domain, result.message)
    else:
        log.info("issued certificate for %s", safe_domain)
    return outcome


def renew(*, dry_run: bool = False) -> dict[str, object]:
    if not available():
        raise CertificateError("certbot is not installed")
    args = ["certbot", "renew", "--non-interactive", "--quiet"]
    if dry_run:
        args.append("--dry-run")
    result = shell.run(args, timeout=900)
    return {"ok": result.ok, "message": result.message, "dry_run": dry_run}


def install_deploy_hook(service_name: str = "panah.service") -> Path:
    """Reload the panel after a successful renewal."""
    hook_dir = paths.ensure(Path("/etc/letsencrypt/renewal-hooks/deploy"), 0o755)
    hook = hook_dir / "panah-reload.sh"
    shell.write_file_locked(
        hook,
        "#!/bin/sh\n"
        "# Installed by Panah: reload the panel after a certificate renewal.\n"
        f"systemctl try-reload-or-restart {service_name} >/dev/null 2>&1 || true\n",
        mode=0o755,
    )
    return hook


def self_signed(domain: str, *, days: int = 825, output_dir: Path | None = None) -> Certificate:
    """Fallback certificate for private deployments without a public domain."""
    import datetime as dt
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    safe_domain = validate_domain(domain)
    directory = paths.ensure(output_dir or (paths.config_dir() / "tls"), 0o750)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, safe_domain)])
    try:
        san = x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(safe_domain))])
    except ValueError:
        san = x509.SubjectAlternativeName([x509.DNSName(safe_domain)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path = directory / "server.crt"
    key_path = directory / "server.key"
    shell.write_file_locked(
        cert_path, certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"), mode=0o644
    )
    shell.write_file_locked(
        key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii"),
        mode=0o600,
    )
    return Certificate(domain=safe_domain, certificate=cert_path, private_key=key_path)
