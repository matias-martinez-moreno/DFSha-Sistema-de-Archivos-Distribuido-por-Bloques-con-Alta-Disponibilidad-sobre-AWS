"""Genera la CA del proyecto, un certificado por nodo y el archivo .env con secretos.

Uso:
    python scripts/generar_seguridad.py                # certs/ y .env con valores por defecto
    python scripts/generar_seguridad.py --datanodes 6  # certificados para más DataNodes
    python scripts/generar_seguridad.py --forzar       # vuelve a generar todo

La CA firma el certificado de cada ControlNode y DataNode. El cliente y los
nodos solo confían en servidores con un certificado firmado por esta CA.
"""

import argparse
import datetime
import ipaddress
import secrets
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parent.parent


def _write_key(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))


def _write_cert(path: Path, cert) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _validity():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now - datetime.timedelta(days=1), now + datetime.timedelta(days=365)


def make_ca(out: Path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DFSha"),
                      x509.NameAttribute(NameOID.COMMON_NAME, "DFSha CA")])
    start, end = _validity()
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write_key(out / "ca.key", key)
    _write_cert(out / "ca.crt", cert)
    return key, cert


def make_node_cert(out: Path, node: str, ca_key, ca_cert) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    start, end = _validity()
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DFSha"),
                                     x509.NameAttribute(NameOID.COMMON_NAME, node)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName(node), x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, key_cert_sign=False,
                                         crl_sign=False, content_commitment=False, data_encipherment=False,
                                         key_agreement=True, encipher_only=False, decipher_only=False),
                           critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                                  ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write_key(out / f"{node}.key", key)
    _write_cert(out / f"{node}.crt", cert)


def node_names(datanodes: int) -> list[str]:
    return ["controlnode1", "controlnode2"] + [f"datanode{i}" for i in range(1, datanodes + 1)]


def generate_certs(out: Path, nodes: list[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ca_key, ca_cert = make_ca(out)
    for node in nodes:
        make_node_cert(out, node, ca_key, ca_cert)


def random_secret() -> str:
    return secrets.token_urlsafe(32)


def generate_env(path: Path, datanodes: int) -> None:
    lines = [
        "# Secretos del clúster local. NO se sube a GitHub.",
        f"JWT_SECRET={random_secret()}",
        f"INTERNAL_KEY={random_secret()}",
        f"BLOCK_TOKEN_SECRET={random_secret()}",
        f"ADMIN_PASSWORD={secrets.token_urlsafe(9)}",
    ]
    lines += [f"STORAGE_KEY_DATANODE{i}={random_secret()}" for i in range(1, datanodes + 1)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--datanodes", type=int, default=6, help="cuántos certificados de DataNode generar")
    parser.add_argument("--salida", default=str(ROOT / "certs"))
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--forzar", action="store_true", help="sobrescribe certificados y .env existentes")
    args = parser.parse_args()

    out = Path(args.salida)
    if (out / "ca.crt").exists() and not args.forzar:
        print(f"Ya existen certificados en {out} (use --forzar para regenerarlos)")
    else:
        generate_certs(out, node_names(args.datanodes))
        print(f"Certificados generados en {out}")

    env_path = Path(args.env)
    if env_path.exists() and not args.forzar:
        print(f"Ya existe {env_path} (use --forzar para regenerarlo)")
    else:
        generate_env(env_path, args.datanodes)
        print(f"Secretos generados en {env_path}")
    admin = [line for line in env_path.read_text(encoding="utf-8").splitlines() if line.startswith("ADMIN_PASSWORD=")]
    if admin:
        print(f"Contraseña del usuario admin: {admin[0].split('=', 1)[1]}")


if __name__ == "__main__":
    main()
