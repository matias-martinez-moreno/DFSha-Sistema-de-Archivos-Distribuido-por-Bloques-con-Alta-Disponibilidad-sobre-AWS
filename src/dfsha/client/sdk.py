"""SDK de Python para usar DFSha. La CLI, las pruebas y Locust usan esta clase.

Ejemplo:
    c = DFSClient(["https://ip-control-1:8000", "https://ip-control-2:8000"], ca_file="certs/ca.crt")
    c.login("ana", "clave")
    c.put("video.mp4", "/users/ana/video.mp4")
    c.get("/users/ana/video.mp4", "copia.mp4")
"""

import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from dfsha.common import paths, security, tls


class DFSError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class DFSClient:
    def __init__(self, control_urls: list[str], ca_file: str | None = None, token: str | None = None,
                 workers: int = 4, timeout: float = 60, failover_wait: float = 30):
        if not control_urls:
            raise DFSError("No hay ControlNodes configurados")
        self.control_urls = [u.rstrip("/") for u in control_urls]
        self.token = token
        self.workers = workers
        self.failover_wait = failover_wait
        self.leader: str | None = None
        self.http = httpx.Client(verify=tls.client_ssl_context(ca_file), timeout=timeout)

    def close(self) -> None:
        self.http.close()

    # Comunicación con el ControlNode líder

    def find_leader(self, deadline: float | None = None) -> str:
        """Pregunta a cada ControlNode cuál es el líder. Si ninguno lo es (failover
        en curso) espera un poco y vuelve a preguntar."""
        deadline = deadline or time.time() + self.failover_wait
        while True:
            for url in self.control_urls:
                try:
                    r = self.http.get(f"{url}/status", timeout=3)
                    if r.status_code == 200 and r.json().get("role") == "leader":
                        self.leader = url
                        return url
                except httpx.HTTPError:
                    continue
            if time.time() > deadline:
                raise DFSError("No hay un ControlNode líder disponible")
            time.sleep(1)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        deadline = time.time() + self.failover_wait
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        while True:
            url = self.leader or self.find_leader(deadline)
            try:
                r = self.http.request(method, f"{url}{path}", headers=headers, **kwargs)
            except httpx.TransportError:
                self.leader = None
                if time.time() > deadline:
                    raise DFSError("No se pudo contactar a ningún ControlNode")
                continue
            if r.status_code == 503 and "not_leader" in r.text:
                self.leader = None
                if time.time() > deadline:
                    raise DFSError("No hay un ControlNode líder disponible")
                time.sleep(0.5)
                continue
            if r.status_code >= 400:
                try:
                    detail = r.json().get("detail", r.text)
                except ValueError:
                    detail = r.text
                raise DFSError(str(detail), r.status_code)
            return r.json()

    # Sesión y usuarios

    def login(self, username: str, password: str) -> str:
        self.token = None
        data = self._request("POST", "/auth/login", json={"username": username, "password": password})
        self.token = data["token"]
        return self.token

    def me(self) -> dict:
        return self._request("GET", "/auth/me")

    def useradd(self, username: str, password: str, groups: list[str] | None = None) -> dict:
        return self._request("POST", "/users", json={"username": username, "password": password,
                                                     "groups": groups or []})

    # Gestión del sistema de archivos (RF1)

    def ls(self, path: str) -> list[dict]:
        return self._request("GET", "/fs/ls", params={"path": path})["entries"]

    def stat(self, path: str) -> dict:
        return self._request("GET", "/fs/stat", params={"path": path})

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except DFSError as exc:
            if exc.status == 404:
                return False
            raise

    def mkdir(self, path: str) -> dict:
        return self._request("POST", "/fs/mkdir", json={"path": path})

    def rmdir(self, path: str) -> dict:
        return self._request("DELETE", "/fs/rmdir", params={"path": path})

    def rm(self, path: str) -> dict:
        return self._request("DELETE", "/fs/rm", params={"path": path})

    def chmod(self, path: str, mode: int | str) -> dict:
        if isinstance(mode, str):
            mode = int(mode, 8)
        return self._request("POST", "/fs/chmod", json={"path": path, "mode": mode})

    def chgrp(self, path: str, group: str) -> dict:
        return self._request("POST", "/fs/chgrp", json={"path": path, "group": group})

    def blocks(self, path: str) -> dict:
        return self._request("GET", "/fs/blocks", params={"path": path})

    def cluster(self) -> dict:
        return self._request("GET", "/cluster")

    # Locks (RF3)

    def lock(self, path: str, mode: str = "r", ttl: float = 300) -> str:
        return self._request("POST", "/locks", json={"path": path, "mode": mode, "ttl": ttl})["lock_id"]

    def unlock(self, lock_id: str) -> None:
        self._request("DELETE", f"/locks/{lock_id}")

    def _unlock_quietly(self, lock_id: str) -> None:
        try:
            self.unlock(lock_id)
        except DFSError:
            pass  # si el lock ya venció no pasa nada

    # Transferencia (RF2)

    def put(self, local_path: str, remote_path: str, lock_id: str | None = None) -> dict:
        size = os.path.getsize(local_path)

        def reader(idx: int, block_size: int) -> bytes:
            with open(local_path, "rb") as f:
                f.seek(idx * block_size)
                return f.read(block_size)

        return self._upload(remote_path, size, reader, lock_id)

    def put_bytes(self, data: bytes, remote_path: str, lock_id: str | None = None) -> dict:
        return self._upload(remote_path, len(data),
                            lambda idx, bs: data[idx * bs:(idx + 1) * bs], lock_id)

    def _upload(self, remote_path: str, size: int, reader, lock_id: str | None) -> dict:
        started = time.time()
        own_lock = lock_id is None
        if own_lock:
            lock_id = self.lock(remote_path, "w", ttl=900)
        try:
            plan = self._request("POST", "/files/plan-write",
                                 json={"path": remote_path, "size": size, "lock_id": lock_id})
            block_size = plan["block_size"]

            def send(block: dict) -> dict:
                data = reader(block["idx"], block_size)
                done = self._send_block(block, data, plan["min_replicas"])
                return {"idx": block["idx"], "block_id": block["block_id"], "size": len(data),
                        "sha256": security.sha256_hex(data), "stored": done}

            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = list(pool.map(send, plan["blocks"]))
            self._request("POST", "/files/commit",
                          json={"upload_id": plan["upload_id"], "lock_id": lock_id, "blocks": results})
        finally:
            if own_lock:
                self._unlock_quietly(lock_id)
        return {"path": remote_path, "size": size, "blocks": len(plan["blocks"]),
                "seconds": round(time.time() - started, 3)}

    def _send_block(self, block: dict, data: bytes, min_replicas: int) -> list[str]:
        """Manda el bloque al primer DataNode del plan con la lista de los demás
        (pipeline). Si el primero no responde, arranca la cadena en el siguiente."""
        checksum = security.sha256_hex(data)
        targets = block["targets"]
        stored: list[str] = []
        for i, target in enumerate(targets):
            pipeline = ",".join(t["internal_url"] for t in targets[i + 1:])
            try:
                r = self.http.put(f"{target['url']}/blocks/{block['block_id']}", content=data, headers={
                    "Authorization": f"Bearer {block['token']}",
                    "X-Block-SHA256": checksum,
                    "X-DFS-Pipeline": pipeline,
                })
                if r.status_code == 200:
                    stored = r.json()["stored"]
                    break
            except httpx.TransportError:
                continue
        if len(stored) < min_replicas:
            raise DFSError(f"El bloque {block['idx']} solo quedó en {len(stored)} réplicas "
                           f"(mínimo {min_replicas})")
        return stored

    def get(self, remote_path: str, local_path: str, lock_id: str | None = None) -> dict:
        started = time.time()
        own_lock = lock_id is None
        if own_lock:
            lock_id = self.lock(remote_path, "r", ttl=900)
        try:
            plan = self._read_plan(remote_path, lock_id)
            tmp_path = local_path + ".part"
            write_lock = threading.Lock()
            with open(tmp_path, "wb") as out:
                def fetch(block: dict) -> None:
                    data = self._fetch_block(block)
                    with write_lock:
                        out.seek(block["idx"] * plan["block_size"])
                        out.write(data)

                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    list(pool.map(fetch, plan["blocks"]))
                out.truncate(plan["size"])
            os.replace(tmp_path, local_path)
        finally:
            if own_lock:
                self._unlock_quietly(lock_id)
        return {"path": remote_path, "size": plan["size"], "blocks": len(plan["blocks"]),
                "seconds": round(time.time() - started, 3)}

    def get_bytes(self, remote_path: str, lock_id: str | None = None) -> bytes:
        own_lock = lock_id is None
        if own_lock:
            lock_id = self.lock(remote_path, "r", ttl=900)
        try:
            plan = self._read_plan(remote_path, lock_id)
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                parts = list(pool.map(self._fetch_block, plan["blocks"]))
            return b"".join(parts)
        finally:
            if own_lock:
                self._unlock_quietly(lock_id)

    def _read_plan(self, remote_path: str, lock_id: str) -> dict:
        return self._request("GET", "/files/plan-read", params={"path": remote_path, "lock_id": lock_id})

    def _fetch_block(self, block: dict) -> bytes:
        """Descarga un bloque de cualquier réplica viva y revisa su checksum."""
        for location in block["locations"]:
            try:
                r = self.http.get(f"{location['url']}/blocks/{block['block_id']}",
                                  headers={"Authorization": f"Bearer {block['token']}"})
            except httpx.TransportError:
                continue
            if r.status_code == 200 and security.sha256_hex(r.content) == block["sha256"]:
                return r.content
        raise DFSError(f"No se pudo leer el bloque {block['idx']} de ninguna réplica")

    # Acceso a archivos (RF3): open/read/write/close

    def open(self, remote_path: str, mode: str = "r", ttl: float = 300) -> "DFSFile":
        return DFSFile(self, remote_path, mode, ttl)


class DFSFile:
    """Archivo abierto en DFSha.

    En modo "r" solo se descargan los bloques que cubren lo que se lee.
    En modo "w" lo escrito se guarda localmente y al cerrar se sube como una
    versión nueva del archivo (el archivo se reemplaza completo, modelo WORM).
    Mientras el archivo está abierto se tiene el lock correspondiente.
    """

    def __init__(self, client: DFSClient, path: str, mode: str, ttl: float):
        if mode not in ("r", "w"):
            raise DFSError("El modo de apertura debe ser r o w")
        self.client = client
        self.path = paths.normalize(path)
        self.mode = mode
        self.lock_id = client.lock(self.path, mode, ttl)
        self.closed = False
        self.pos = 0
        if mode == "r":
            try:
                self.plan = client._read_plan(self.path, self.lock_id)
            except DFSError:
                client._unlock_quietly(self.lock_id)
                raise
            self._cache: dict[int, bytes] = {}
        else:
            self._buffer = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
            self._buffer_lock = threading.Lock()

    @property
    def size(self) -> int:
        return self.plan["size"] if self.mode == "r" else self._buffer.tell()

    def seek(self, offset: int) -> None:
        self.pos = max(0, offset)

    def read(self, n: int = -1) -> bytes:
        data = self.pread(self.pos, n)
        self.pos += len(data)
        return data

    def pread(self, offset: int, n: int = -1) -> bytes:
        self._check("r")
        size = self.plan["size"]
        if offset >= size or n == 0:
            return b""
        end = size if n < 0 else min(size, offset + n)
        bs = self.plan["block_size"]
        first, last = offset // bs, (end - 1) // bs
        needed = [b for b in self.plan["blocks"] if first <= b["idx"] <= last and b["idx"] not in self._cache]
        with ThreadPoolExecutor(max_workers=self.client.workers) as pool:
            for block, data in zip(needed, pool.map(self.client._fetch_block, needed)):
                self._cache[block["idx"]] = data
        joined = b"".join(self._cache[i] for i in range(first, last + 1))
        start = offset - first * bs
        return joined[start:start + (end - offset)]

    def write(self, data: bytes | str) -> int:
        self._check("w")
        if isinstance(data, str):
            data = data.encode()
        return self._buffer.write(data)

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self.mode == "w":
                size = self._buffer.tell()

                def reader(idx: int, bs: int) -> bytes:
                    with self._buffer_lock:
                        self._buffer.seek(idx * bs)
                        return self._buffer.read(bs)

                self.client._upload(self.path, size, reader, self.lock_id)
                self._buffer.close()
        finally:
            self.closed = True
            self.client._unlock_quietly(self.lock_id)

    def _check(self, mode: str) -> None:
        if self.closed:
            raise DFSError("El archivo ya está cerrado")
        if self.mode != mode:
            raise DFSError(f"El archivo está abierto en modo {self.mode}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type and self.mode == "w":
            # Si hubo un error escribiendo no se sube nada, solo se suelta el lock
            self.closed = True
            self.client._unlock_quietly(self.lock_id)
        else:
            self.close()
