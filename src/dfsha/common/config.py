"""Lectura de la configuración desde variables de entorno.

Todos los nodos se configuran así para que el mismo código sirva en local,
en Docker Compose y en las EC2 que crea Terraform.
"""

import os


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_int(name: str, default: int) -> int:
    return int(env(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(env(name, str(default)))


def env_list(name: str, default: str = "") -> list[str]:
    raw = env(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def required(name: str) -> str:
    value = env(name)
    if value is None:
        raise RuntimeError(f"Falta la variable de entorno {name}")
    return value
