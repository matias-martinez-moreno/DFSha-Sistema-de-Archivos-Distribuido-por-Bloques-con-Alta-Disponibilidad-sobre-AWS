"""Funciones de seguridad compartidas por todos los componentes.

Aquí están las contraseñas (bcrypt), los tokens (JWT), el cifrado de los
bloques en disco (AES-256-GCM) y la validación de llamadas internas.
"""

import hashlib
import hmac
import ipaddress
import os
import time

import bcrypt
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, Request


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Contraseñas de usuario

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# Tokens JWT (sesión de usuario y permisos de bloque)

def create_token(secret: str, claims: dict, ttl_seconds: int) -> str:
    payload = dict(claims)
    payload["exp"] = int(time.time()) + ttl_seconds
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(secret: str, token: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"])


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Falta el token de autenticación")
    return authorization[len("Bearer "):]


# Cifrado de bloques en reposo

class BlockCipher:
    """Cifra cada bloque con AES-256-GCM.

    La llave sale del secreto propio de cada DataNode. El id del bloque se usa
    como dato asociado, así un bloque cifrado no se puede hacer pasar por otro.
    """

    def __init__(self, secret: str):
        self._aes = AESGCM(hashlib.sha256(secret.encode()).digest())

    def encrypt(self, data: bytes, block_id: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, data, block_id.encode())

    def decrypt(self, blob: bytes, block_id: str) -> bytes:
        return self._aes.decrypt(blob[:12], blob[12:], block_id.encode())


# Llamadas internas entre nodos

def ip_in_cidrs(ip: str, cidrs: list[str]) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)


def require_internal(request: Request, given_key: str | None, expected_key: str, cidrs: list[str]) -> None:
    """Un endpoint interno solo acepta llamadas con la llave interna y desde la red privada."""
    if not given_key or not hmac.compare_digest(given_key, expected_key):
        raise HTTPException(status_code=403, detail="Llave interna inválida")
    client_ip = request.client.host if request.client else ""
    if not ip_in_cidrs(client_ip, cidrs):
        raise HTTPException(status_code=403, detail="Origen fuera de la red interna")
