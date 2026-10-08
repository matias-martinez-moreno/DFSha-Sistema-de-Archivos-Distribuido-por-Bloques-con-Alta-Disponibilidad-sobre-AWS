"""Manejo de rutas del sistema de archivos lógico (siempre estilo Linux)."""

import posixpath


def normalize(path: str) -> str:
    if not path or not path.startswith("/"):
        raise ValueError("La ruta debe ser absoluta, por ejemplo /users/ana/notas.txt")
    clean = posixpath.normpath(path)
    # normpath deja "//" al inicio intacto, lo unificamos a "/"
    if clean.startswith("//"):
        clean = "/" + clean.lstrip("/")
    return clean


def resolve(cwd: str, path: str) -> str:
    """Convierte una ruta relativa al directorio actual en absoluta."""
    if path.startswith("/"):
        return normalize(path)
    return normalize(posixpath.join(cwd, path))


def parent(path: str) -> str:
    return posixpath.dirname(path) or "/"


def name(path: str) -> str:
    return posixpath.basename(path)
