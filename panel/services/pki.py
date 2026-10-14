"""Certification authority for OpenVPN inbounds.

The whole PKI is produced with the ``cryptography`` library rather than by
shelling out to easy-rsa: it is deterministic, testable off-server, needs no
extra package on the node, and lets the panel rebuild a CRL from the database
at any time.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import secrets
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ..core.logging import get_logger

log = get_logger("pki")

CA_VALIDITY_DAYS = 3650
SERVER_VALIDITY_DAYS = 1825
CLIENT_VALIDITY_DAYS = 825
KEY_SIZE_RSA = 3072

# Not before is backdated slightly to tolerate clock skew on fresh VPS images.
_SKEW = dt.timedelta(minutes=15)


@dataclass(slots=True)
class KeyPair:
    certificate_pem: str
    private_key_pem: str
    serial: str
    not_after: dt.datetime


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _serial() -> int:
    return x509.random_serial_number()


def generate_rsa_key(bits: int = KEY_SIZE_RSA) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def generate_ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def private_key_pem(key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def load_private_key(pem: str):
    return serialization.load_pem_private_key(pem.encode("ascii"), password=None)


def load_certificate(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode("ascii"))


def _name(common_name: str, organisation: str = "Panah VPN") -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organisation),
        ]
    )


def _san_entries(hostnames: list[str]) -> list[x509.GeneralName]:
    entries: list[x509.GeneralName] = []
    for host in hostnames:
        candidate = host.strip()
        if not candidate:
            continue
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(candidate)))
        except ValueError:
            entries.append(x509.DNSName(candidate))
    return entries


def create_ca(common_name: str = "Panah CA", organisation: str = "Panah VPN") -> KeyPair:
    key = generate_rsa_key()
    subject = _name(common_name, organisation)
    now = _now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(_serial())
        .not_valid_before(now - _SKEW)
        .not_valid_after(now + dt.timedelta(days=CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA512())
    )
    return KeyPair(
        certificate_pem=certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        private_key_pem=private_key_pem(key),
        serial=format(certificate.serial_number, "x"),
        not_after=certificate.not_valid_after_utc,
    )


def create_server_certificate(
    ca_cert_pem: str,
    ca_key_pem: str,
    common_name: str,
    hostnames: list[str] | None = None,
) -> KeyPair:
    ca_cert = load_certificate(ca_cert_pem)
    ca_key = load_private_key(ca_key_pem)
    key = generate_rsa_key()
    now = _now()
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(_serial())
        .not_valid_before(now - _SKEW)
        .not_valid_after(now + dt.timedelta(days=SERVER_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    )
    entries = _san_entries(hostnames or [])
    if entries:
        builder = builder.add_extension(x509.SubjectAlternativeName(entries), critical=False)
    certificate = builder.sign(ca_key, hashes.SHA512())
    return KeyPair(
        certificate_pem=certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        private_key_pem=private_key_pem(key),
        serial=format(certificate.serial_number, "x"),
        not_after=certificate.not_valid_after_utc,
    )


def create_client_certificate(
    ca_cert_pem: str,
    ca_key_pem: str,
    common_name: str,
    validity_days: int = CLIENT_VALIDITY_DAYS,
) -> KeyPair:
    ca_cert = load_certificate(ca_cert_pem)
    ca_key = load_private_key(ca_key_pem)
    key = generate_rsa_key(2048)
    now = _now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(_serial())
        .not_valid_before(now - _SKEW)
        .not_valid_after(now + dt.timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA512())
    )
    return KeyPair(
        certificate_pem=certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        private_key_pem=private_key_pem(key),
        serial=format(certificate.serial_number, "x"),
        not_after=certificate.not_valid_after_utc,
    )


def certificate_serial(cert_pem: str) -> str:
    return format(load_certificate(cert_pem).serial_number, "x")


def certificate_not_after(cert_pem: str) -> dt.datetime:
    return load_certificate(cert_pem).not_valid_after_utc


def certificate_common_name(cert_pem: str) -> str:
    attributes = load_certificate(cert_pem).subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return attributes[0].value if attributes else ""


def build_crl(
    ca_cert_pem: str,
    ca_key_pem: str,
    revoked: list[tuple[str, dt.datetime | None]],
    *,
    validity_days: int = 30,
) -> str:
    """Build a CRL from ``(serial_hex, revocation_time)`` pairs."""
    ca_cert = load_certificate(ca_cert_pem)
    ca_key = load_private_key(ca_key_pem)
    now = _now()
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(now - _SKEW)
        .next_update(now + dt.timedelta(days=validity_days))
    )
    for serial_hex, when in revoked:
        try:
            serial_number = int(serial_hex, 16)
        except (TypeError, ValueError):
            log.warning("skipping malformed serial in CRL: %r", serial_hex)
            continue
        revoked_cert = (
            x509.RevokedCertificateBuilder()
            .serial_number(serial_number)
            .revocation_date(when or now)
            .build()
        )
        builder = builder.add_revoked_certificate(revoked_cert)

    crl = builder.sign(ca_key, hashes.SHA256())
    return crl.public_bytes(serialization.Encoding.PEM).decode("ascii")


def generate_tls_crypt_key() -> str:
    """Reproduce ``openvpn --genkey secret`` output without the binary."""
    material = secrets.token_bytes(256)
    hexed = material.hex()
    lines = [hexed[i : i + 32] for i in range(0, len(hexed), 32)]
    body = "\n".join(lines)
    return (
        "#\n"
        "# 2048 bit OpenVPN static key\n"
        "#\n"
        "-----BEGIN OpenVPN Static key V1-----\n"
        f"{body}\n"
        "-----END OpenVPN Static key V1-----\n"
    )


def validate_tls_crypt_key(content: str) -> bool:
    return "BEGIN OpenVPN Static key V1" in content and "END OpenVPN Static key V1" in content


def fingerprint_sha256(cert_pem: str) -> str:
    certificate = load_certificate(cert_pem)
    digest = certificate.fingerprint(hashes.SHA256())
    return ":".join(f"{byte:02X}" for byte in digest)
