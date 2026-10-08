"""Locks de lectura/escritura sobre rutas, con tiempo de vencimiento (lease).

Funciona como el problema clásico de lectores y escritores: muchos pueden leer
a la vez, pero si alguien escribe nadie más puede leer ni escribir ese archivo.
Cada lock vence solo, así un cliente que se cae no deja el archivo bloqueado.
"""

import threading
import time
import uuid


class LockConflict(Exception):
    pass


class LockManager:
    def __init__(self):
        self._by_path: dict[str, dict] = {}  # ruta -> {"mode": "r"|"w", "holders": {lock_id: (usuario, vence)}}
        self._by_id: dict[str, str] = {}     # lock_id -> ruta
        self._mu = threading.Lock()

    def _purge(self, path: str) -> dict | None:
        entry = self._by_path.get(path)
        if not entry:
            return None
        now = time.time()
        for lock_id, (_, expires) in list(entry["holders"].items()):
            if expires < now:
                del entry["holders"][lock_id]
                self._by_id.pop(lock_id, None)
        if not entry["holders"]:
            del self._by_path[path]
            return None
        return entry

    def acquire(self, path: str, mode: str, user: str, ttl: float) -> tuple[str, float]:
        if mode not in ("r", "w"):
            raise ValueError("El modo del lock debe ser r o w")
        with self._mu:
            entry = self._purge(path)
            if entry and (mode == "w" or entry["mode"] == "w"):
                holders = sorted({u for u, _ in entry["holders"].values()})
                raise LockConflict(f"{path} está bloqueado en modo {entry['mode']} por {', '.join(holders)}")
            if not entry:
                entry = self._by_path[path] = {"mode": mode, "holders": {}}
            lock_id = uuid.uuid4().hex
            expires = time.time() + ttl
            entry["holders"][lock_id] = (user, expires)
            self._by_id[lock_id] = path
            return lock_id, expires

    def release(self, lock_id: str, user: str) -> bool:
        with self._mu:
            path = self._by_id.get(lock_id)
            if not path:
                return False
            entry = self._by_path.get(path)
            if not entry or entry["holders"].get(lock_id, (None,))[0] != user:
                return False
            del entry["holders"][lock_id]
            del self._by_id[lock_id]
            if not entry["holders"]:
                del self._by_path[path]
            return True

    def holds(self, lock_id: str, path: str, mode: str, user: str) -> bool:
        """True si ese lock está vigente, es de ese usuario, es sobre esa ruta y alcanza para el modo pedido."""
        with self._mu:
            entry = self._purge(path)
            if not entry or lock_id not in entry["holders"]:
                return False
            owner, _ = entry["holders"][lock_id]
            if owner != user:
                return False
            return mode == "r" or entry["mode"] == "w"

    def is_locked(self, path: str) -> bool:
        with self._mu:
            return self._purge(path) is not None

    def count(self) -> int:
        with self._mu:
            for path in list(self._by_path):
                self._purge(path)
            return len(self._by_id)
