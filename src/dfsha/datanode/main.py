"""DataNode: guarda bloques cifrados, los reenvía en pipeline y reporta al ControlNode.

API pública (cliente):
    PUT /blocks/{id}   subir un bloque (requiere permiso de bloque firmado por el ControlNode)
    GET /blocks/{id}   descargar un bloque
API interna (otros nodos de la VPC):
    PUT /internal/blocks/{id}   recibir una réplica desde otro DataNode
"""

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response

from dfsha.common import config, security, tls
from dfsha.datanode.storage import BLOCK_ID, BlockStore

log = logging.getLogger("datanode")


class Settings:
    def __init__(self):
        self.node_id = config.env("NODE_ID", "datanode1")
        self.port = config.env_int("PORT", 8100)
        self.public_url = config.required("PUBLIC_URL")
        self.internal_url = config.env("INTERNAL_URL", self.public_url)
        self.control_urls = config.env_list("CONTROL_URLS")
        self.data_dir = Path(config.env("DATA_DIR", f"./data/{self.node_id}"))
        self.internal_key = config.required("INTERNAL_KEY")
        self.block_token_secret = config.required("BLOCK_TOKEN_SECRET")
        self.storage_key = config.required("STORAGE_KEY")
        self.ca_file = config.env("TLS_CA")
        self.cert_file = config.env("TLS_CERT")
        self.key_file = config.env("TLS_KEY")
        self.internal_cidrs = config.env_list(
            "INTERNAL_CIDRS", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32")
        self.heartbeat_interval = config.env_float("HEARTBEAT_INTERVAL", 3)


class DataNode:
    def __init__(self, settings: Settings):
        self.s = settings
        self.store = BlockStore(settings.data_dir, security.BlockCipher(settings.storage_key))
        self.http = httpx.Client(verify=tls.client_ssl_context(settings.ca_file), timeout=30)
        self.workers = ThreadPoolExecutor(max_workers=4)
        self.stop = threading.Event()

    # Escritura en pipeline: guardo mi copia y la paso al siguiente de la lista

    def store_and_forward(self, block_id: str, data: bytes, checksum: str, pipeline: list[str]) -> list[str]:
        if not BLOCK_ID.match(block_id):
            raise HTTPException(status_code=400, detail="Id de bloque inválido")
        if security.sha256_hex(data) != checksum:
            raise HTTPException(status_code=400, detail="El checksum del bloque no coincide")
        self.store.put(block_id, data)
        stored = [self.s.node_id]
        # Si el siguiente nodo no responde se intenta con el que sigue, así una
        # réplica caída no corta la cadena completa.
        for i, next_url in enumerate(pipeline):
            try:
                r = self.http.put(
                    f"{next_url}/internal/blocks/{block_id}",
                    content=data,
                    headers={
                        "X-Internal-Key": self.s.internal_key,
                        "X-Block-SHA256": checksum,
                        "X-DFS-Pipeline": ",".join(pipeline[i + 1:]),
                    },
                )
                r.raise_for_status()
                stored += r.json()["stored"]
                break
            except httpx.HTTPError as exc:
                log.warning("No se pudo reenviar el bloque %s a %s: %s", block_id, next_url, exc)
        return stored

    # Heartbeat y reporte de bloques hacia los ControlNodes

    def heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            payload = {
                "node_id": self.s.node_id,
                "public_url": self.s.public_url,
                "internal_url": self.s.internal_url,
                "blocks": self.store.list_ids(),
                "used_bytes": self.store.used_bytes(),
            }
            for url in self.s.control_urls:
                try:
                    r = self.http.post(f"{url}/internal/heartbeat", json=payload,
                                       headers={"X-Internal-Key": self.s.internal_key}, timeout=5)
                    if r.status_code == 200:
                        self.run_commands(r.json())
                except httpx.HTTPError:
                    pass  # ese ControlNode está caído, el otro sí recibe el heartbeat
            self.stop.wait(self.s.heartbeat_interval)

    def run_commands(self, answer: dict) -> None:
        """El ControlNode líder responde el heartbeat con órdenes: borrar o re-replicar bloques."""
        for block_id in answer.get("delete", []):
            self.store.delete(block_id)
            log.info("Bloque %s borrado por orden del ControlNode", block_id)
        for order in answer.get("replicate", []):
            self.workers.submit(self.replicate_to, order["block_id"], order["target_url"])

    def replicate_to(self, block_id: str, target_url: str) -> None:
        data = self.store.get(block_id)
        if data is None:
            return
        try:
            r = self.http.put(
                f"{target_url}/internal/blocks/{block_id}",
                content=data,
                headers={
                    "X-Internal-Key": self.s.internal_key,
                    "X-Block-SHA256": security.sha256_hex(data),
                    "X-DFS-Pipeline": "",
                },
            )
            r.raise_for_status()
            log.info("Bloque %s re-replicado en %s", block_id, target_url)
        except httpx.HTTPError as exc:
            log.warning("Falló la re-replicación de %s hacia %s: %s", block_id, target_url, exc)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    node = DataNode(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        thread = threading.Thread(target=node.heartbeat_loop, daemon=True)
        thread.start()
        yield
        node.stop.set()

    app = FastAPI(title=f"DFSha DataNode ({settings.node_id})", lifespan=lifespan)
    app.state.node = node

    def check_block_permission(authorization: str | None, block_id: str, op: str) -> None:
        token = security.bearer_token(authorization)
        try:
            claims = security.decode_token(settings.block_token_secret, token)
        except Exception:
            raise HTTPException(status_code=401, detail="Permiso de bloque inválido o vencido")
        if claims.get("bid") != block_id or op not in claims.get("op", ""):
            raise HTTPException(status_code=403, detail="El permiso no corresponde a este bloque")

    def parse_pipeline(header: str | None) -> list[str]:
        return [u for u in (header or "").split(",") if u]

    @app.put("/blocks/{block_id}")
    async def put_block(block_id: str, request: Request,
                        authorization: str | None = Header(None),
                        x_block_sha256: str = Header(...),
                        x_dfs_pipeline: str | None = Header(None)):
        check_block_permission(authorization, block_id, "w")
        data = await request.body()
        stored = await asyncio.to_thread(node.store_and_forward, block_id, data,
                                         x_block_sha256, parse_pipeline(x_dfs_pipeline))
        return {"stored": stored}

    @app.get("/blocks/{block_id}")
    async def get_block(block_id: str, authorization: str | None = Header(None)):
        check_block_permission(authorization, block_id, "r")
        try:
            data = await asyncio.to_thread(node.store.get, block_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Id de bloque inválido")
        if data is None:
            raise HTTPException(status_code=404, detail="Este DataNode no tiene el bloque")
        return Response(content=data, media_type="application/octet-stream")

    @app.put("/internal/blocks/{block_id}")
    async def put_replica(block_id: str, request: Request,
                          x_internal_key: str | None = Header(None),
                          x_block_sha256: str = Header(...),
                          x_dfs_pipeline: str | None = Header(None)):
        security.require_internal(request, x_internal_key, settings.internal_key, settings.internal_cidrs)
        data = await request.body()
        stored = await asyncio.to_thread(node.store_and_forward, block_id, data,
                                         x_block_sha256, parse_pipeline(x_dfs_pipeline))
        return {"stored": stored}

    @app.get("/status")
    async def status():
        blocks = await asyncio.to_thread(node.store.list_ids)
        return {"node_id": settings.node_id, "role": "datanode", "blocks": len(blocks),
                "used_bytes": await asyncio.to_thread(node.store.used_bytes)}

    return app
