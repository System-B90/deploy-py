"""TLS for the app's nginx proxy.

A leaf signed by a local "System-B90 Local Dev CA" that is created once and
reused, so trusting ca.crt in the OS/browser survives leaf regeneration
(peek-a-boo's approach). Leaves carry SANs — browsers ignore the CN, which is
all Bluz's old `openssl req -subj /CN=...` certificate had.

An operator-supplied certificate that already covers the hostname and is not
about to expire is kept as-is, whoever issued it.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_NAME = "System-B90 Local Dev CA"
RENEW_BEFORE = timedelta(days=14)


def _write(path: Path, data: bytes, private: bool = False) -> None:
    path.write_bytes(data)
    if private:
        try:
            path.chmod(0o600)
        except OSError:  # Windows: chmod is mostly a no-op
            pass


def _key_pem(key) -> bytes:
    return key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "IL"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "System-B90"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _usage(**enabled) -> x509.KeyUsage:
    flags = {
        "digital_signature": False,
        "content_commitment": False,
        "key_encipherment": False,
        "data_encipherment": False,
        "key_agreement": False,
        "key_cert_sign": False,
        "crl_sign": False,
        "encipher_only": False,
        "decipher_only": False,
    }
    flags.update(enabled)
    return x509.KeyUsage(**flags)


def load_or_create_ca(cert_path: Path, key_path: Path):
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), None)
        return cert, key
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    name = _name(CA_NAME)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_usage(key_cert_sign=True, crl_sign=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write(cert_path, cert.public_bytes(Encoding.PEM))
    _write(key_path, _key_pem(key), private=True)
    return cert, key


def issue_leaf(
    cert_path: Path, key_path: Path, ca_cert, ca_key, hostname: str, alt_names: list[str]
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans: list[x509.GeneralName] = []
    for name in dict.fromkeys([hostname] + alt_names):
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            sans.append(x509.DNSName(name))
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(hostname))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        # 397 days: the longest leaf lifetime browsers accept.
        .not_valid_after(now + timedelta(days=397))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_usage(digital_signature=True, key_encipherment=True), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write(cert_path, cert.public_bytes(Encoding.PEM))
    _write(key_path, _key_pem(key), private=True)


def covers(cert_path: Path, hostname: str) -> bool:
    """True if the certificate names `hostname` (SAN or CN) and is not expiring."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return False
    if cert.not_valid_after_utc < datetime.now(timezone.utc) + RENEW_BEFORE:
        return False
    try:
        names = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    names += [a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    return hostname in names


def ensure_certificate(
    ssl_dir: str | Path,
    hostname: str,
    cert_name: str = "cert.pem",
    key_name: str = "key.pem",
    alt_names: list[str] | None = None,
    ca_cert_name: str = "ca.crt",
    ca_key_name: str = "ca.key",
) -> Path:
    """Make `ssl_dir/cert_name` a valid certificate for `hostname`. Returns the CA path."""
    root = Path(ssl_dir)
    root.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = root / cert_name, root / key_name
    ca_cert_path = root / ca_cert_name
    if key_path.exists() and covers(cert_path, hostname):
        print(f"[OK] Using existing certificate for {hostname} ({cert_path})")
        return ca_cert_path
    ca_cert, ca_key = load_or_create_ca(ca_cert_path, root / ca_key_name)
    names = ["localhost", "127.0.0.1"] + list(alt_names or [])
    issue_leaf(cert_path, key_path, ca_cert, ca_key, hostname, names)
    print(f"[OK] Issued {cert_path} for {hostname} (signed by {CA_NAME})")
    print(
        f"     Trust {ca_cert_path} once to silence browser warnings "
        f"(Windows: certutil -addstore -user Root {ca_cert_path})"
    )
    return ca_cert_path
