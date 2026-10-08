"""ControlNode: API de metadatos del DFSha.

El ControlNode nunca mueve datos de archivos. Solo responde dónde están o
dónde deben ir los bloques, controla permisos y locks, y guarda el árbol de
directorios. Los bloques viajan directo entre el cliente y los DataNodes.
"""

import logging
import math
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from dfsha.common import config, paths, security
from dfsha.controlnode.cluster import Cluster, ClusterSettings, NoDataNodes, NotLeader
from dfsha.controlnode.db import MetadataDB
from dfsha.controlnode.locks import LockConflict, LockManager

log = logging.getLogger("controlnode")

USERNAME = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
GROUPNAME = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


class Settings:
    def __init__(self):
        self.port = config.env_int("PORT", 8000)
        self.db_path = config.env("DB_PATH", "./data/metadata.db")
        self.jwt_secret = config.required("JWT_SECRET")
        self.jwt_ttl = config.env_int("JWT_TTL", 8 * 3600)
        self.block_token_secret = config.required("BLOCK_TOKEN_SECRET")
        self.block_token_ttl = config.env_int("BLOCK_TOKEN_TTL", 900)
        self.block_size = config.env_int("BLOCK_SIZE", 4 * 1024 * 1024)
        self.internal_cidrs = config.env_list(
            "INTERNAL_CIDRS", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1/32")
        self.cert_file = config.env("TLS_CERT")
        self.key_file = config.env("TLS_KEY")
        self.cluster = ClusterSettings()


# Cuerpos de las peticiones

class LoginIn(BaseModel):
    username: str
    password: str


class UserIn(BaseModel):
    username: str
    password: str
    groups: list[str] = []


class PathIn(BaseModel):
    path: str


class ChmodIn(BaseModel):
    path: str
    mode: int


class ChgrpIn(BaseModel):
    path: str
    group: str


class LockIn(BaseModel):
    path: str
    mode: str
    ttl: float = 300


class PlanWriteIn(BaseModel):
    path: str
    size: int
    lock_id: str


class BlockDone(BaseModel):
    idx: int
    block_id: str
    size: int
    sha256: str
    stored: list[str]


class CommitIn(BaseModel):
    upload_id: str
    lock_id: str
    blocks: list[BlockDone]


class HeartbeatIn(BaseModel):
    node_id: str
    public_url: str
    internal_url: str
    blocks: list[str]
    used_bytes: int


class ReplicateIn(BaseModel):
    epoch: int
    seq: int
    op: dict
    leader_url: str
    node_id: str


# Permisos tipo Linux: dueño, grupo y otros con bits rwx

def can(user: dict, inode: dict, perm: str) -> bool:
    if user["is_admin"]:
        return True
    bit = {"r": 4, "w": 2, "x": 1}[perm]
    if inode["owner"] == user["username"]:
        shift = 6
    elif inode["grp"] in user["groups"]:
        shift = 3
    else:
        shift = 0
    return bool((inode["mode"] >> shift) & bit)


def mode_string(kind: str, mode: int) -> str:
    out = "d" if kind == "dir" else "-"
    for shift in (6, 3, 0):
        bits = (mode >> shift) & 7
        out += ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-")
    return out


def inode_view(inode: dict) -> dict:
    return {
        "path": inode["path"],
        "name": paths.name(inode["path"]) or "/",
        "kind": inode["kind"],
        "owner": inode["owner"],
        "group": inode["grp"],
        "mode": inode["mode"],
        "mode_str": mode_string(inode["kind"], inode["mode"]),
        "size": inode["size"],
        "version": inode["version"],
        "mtime": inode["mtime"],
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    db = MetadataDB(settings.db_path)
    cluster = Cluster(settings.cluster, db)
    locks = LockManager()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        cluster.start()
        yield
        cluster.stop.set()

    app = FastAPI(title=f"DFSha ControlNode ({settings.cluster.node_id})", lifespan=lifespan)
    app.state.cluster = cluster

    # Dependencias comunes

    def leader_only():
        if cluster.role != "leader":
            raise HTTPException(status_code=503, detail="not_leader")

    def current_user(authorization: str | None = Header(None)) -> dict:
        token = security.bearer_token(authorization)
        try:
            claims = security.decode_token(settings.jwt_secret, token)
        except Exception:
            raise HTTPException(status_code=401, detail="Sesión inválida o vencida, vuelva a hacer login")
        user = db.get_user(claims.get("sub", ""))
        if not user:
            raise HTTPException(status_code=401, detail="El usuario ya no existe")
        return user

    def internal(request: Request, x_internal_key: str | None = Header(None)):
        security.require_internal(request, x_internal_key, settings.cluster.internal_key, settings.internal_cidrs)

    def clean(path: str) -> str:
        try:
            return paths.normalize(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    def need_inode(path: str, kind: str | None = None) -> dict:
        inode = db.get_inode(path)
        if not inode:
            raise HTTPException(status_code=404, detail=f"No existe {path}")
        if kind and inode["kind"] != kind:
            what = "un directorio" if kind == "dir" else "un archivo"
            raise HTTPException(status_code=400, detail=f"{path} no es {what}")
        return inode

    def need_perm(user: dict, inode: dict, perm: str) -> None:
        if not can(user, inode, perm):
            raise HTTPException(status_code=403, detail=f"Permiso denegado ({perm}) sobre {inode['path']}")

    def submit(op: dict) -> None:
        try:
            cluster.submit(op)
        except NotLeader:
            raise HTTPException(status_code=503, detail="not_leader")

    def block_token(block_id: str, op: str) -> str:
        return security.create_token(settings.block_token_secret, {"bid": block_id, "op": op},
                                     settings.block_token_ttl)

    deps = [Depends(leader_only)]

    # Autenticación y usuarios

    @app.post("/auth/login", dependencies=deps)
    def login(body: LoginIn):
        user = db.get_user(body.username)
        if not user or not security.check_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")
        token = security.create_token(settings.jwt_secret, {"sub": user["username"]}, settings.jwt_ttl)
        return {"token": token, "username": user["username"], "expires_in": settings.jwt_ttl}

    @app.get("/auth/me", dependencies=deps)
    def me(user: dict = Depends(current_user)):
        return {"username": user["username"], "groups": user["groups"], "is_admin": user["is_admin"],
                "home": "/" if user["is_admin"] else f"/users/{user['username']}"}

    @app.post("/users", dependencies=deps)
    def create_user(body: UserIn, user: dict = Depends(current_user)):
        if not user["is_admin"]:
            raise HTTPException(status_code=403, detail="Solo el administrador crea usuarios")
        if not USERNAME.match(body.username):
            raise HTTPException(status_code=400, detail="Usuario inválido: minúsculas, números o _, de 3 a 32")
        if len(body.password) < 6:
            raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres")
        groups = [body.username] + [g for g in body.groups if g != body.username]
        if not all(GROUPNAME.match(g) for g in groups):
            raise HTTPException(status_code=400, detail="Nombre de grupo inválido")
        if db.get_user(body.username) or db.get_inode(f"/users/{body.username}"):
            raise HTTPException(status_code=409, detail="El usuario ya existe")
        submit({"type": "create_user", "username": body.username, "groups": groups,
                "password_hash": security.hash_password(body.password), "mtime": time.time()})
        return {"username": body.username, "groups": groups, "home": f"/users/{body.username}"}

    # Gestión del sistema de archivos (RF1)

    @app.get("/fs/ls", dependencies=deps)
    def ls(path: str, user: dict = Depends(current_user)):
        path = clean(path)
        inode = need_inode(path)
        need_perm(user, inode, "r")
        if inode["kind"] == "file":
            return {"path": path, "entries": [inode_view(inode)]}
        return {"path": path, "entries": [inode_view(i) for i in db.list_dir(path)]}

    @app.get("/fs/stat", dependencies=deps)
    def stat(path: str, user: dict = Depends(current_user)):
        path = clean(path)
        inode = need_inode(path)
        if path != "/":
            need_perm(user, need_inode(paths.parent(path)), "r")
        return inode_view(inode)

    @app.post("/fs/mkdir", dependencies=deps)
    def mkdir(body: PathIn, user: dict = Depends(current_user)):
        path = clean(body.path)
        if db.get_inode(path):
            raise HTTPException(status_code=409, detail=f"Ya existe {path}")
        parent = need_inode(paths.parent(path), "dir")
        need_perm(user, parent, "w")
        # Lo nuevo hereda el grupo de la carpeta donde se crea (como setgid en Linux)
        submit({"type": "mkdir", "path": path, "owner": user["username"], "grp": parent["grp"],
                "mode": 0o755, "mtime": time.time()})
        return {"path": path}

    @app.delete("/fs/rmdir", dependencies=deps)
    def rmdir(path: str, user: dict = Depends(current_user)):
        path = clean(path)
        if path in ("/", "/users"):
            raise HTTPException(status_code=400, detail="Ese directorio no se puede borrar")
        need_inode(path, "dir")
        need_perm(user, need_inode(paths.parent(path)), "w")
        if db.has_children(path):
            raise HTTPException(status_code=409, detail=f"{path} no está vacío")
        submit({"type": "rmdir", "path": path})
        return {"path": path}

    @app.delete("/fs/rm", dependencies=deps)
    def rm(path: str, user: dict = Depends(current_user)):
        path = clean(path)
        need_inode(path, "file")
        need_perm(user, need_inode(paths.parent(path)), "w")
        if locks.is_locked(path):
            raise HTTPException(status_code=409, detail=f"{path} está en uso (tiene un lock activo)")
        submit({"type": "rm", "path": path, "mtime": time.time()})
        return {"path": path}

    @app.post("/fs/chmod", dependencies=deps)
    def chmod(body: ChmodIn, user: dict = Depends(current_user)):
        path = clean(body.path)
        inode = need_inode(path)
        if not (user["is_admin"] or inode["owner"] == user["username"]):
            raise HTTPException(status_code=403, detail="Solo el dueño puede cambiar los permisos")
        if not 0 <= body.mode <= 0o777:
            raise HTTPException(status_code=400, detail="Modo inválido")
        submit({"type": "chmod", "path": path, "mode": body.mode})
        return {"path": path, "mode": body.mode}

    @app.post("/fs/chgrp", dependencies=deps)
    def chgrp(body: ChgrpIn, user: dict = Depends(current_user)):
        path = clean(body.path)
        inode = need_inode(path)
        if not (user["is_admin"] or (inode["owner"] == user["username"] and body.group in user["groups"])):
            raise HTTPException(status_code=403, detail="Solo el dueño, y a un grupo al que pertenezca")
        submit({"type": "chgrp", "path": path, "grp": body.group})
        return {"path": path, "group": body.group}

    @app.get("/fs/blocks", dependencies=deps)
    def blocks_of(path: str, user: dict = Depends(current_user)):
        path = clean(path)
        inode = need_inode(path, "file")
        need_perm(user, inode, "r")
        return {"path": path, "size": inode["size"], "version": inode["version"],
                "block_size": settings.block_size,
                "blocks": [{**b, "locations": [loc["id"] for loc in cluster.locations(b["block_id"])]}
                           for b in db.file_blocks(path)]}

    # Locks (RF3)

    @app.post("/locks", dependencies=deps)
    def lock(body: LockIn, user: dict = Depends(current_user)):
        path = clean(body.path)
        inode = db.get_inode(path)
        if body.mode == "r":
            inode = need_inode(path, "file")
            need_perm(user, inode, "r")
        elif body.mode == "w":
            if inode:
                if inode["kind"] != "file":
                    raise HTTPException(status_code=400, detail=f"{path} es un directorio")
                need_perm(user, inode, "w")
            else:
                need_perm(user, need_inode(paths.parent(path), "dir"), "w")
        else:
            raise HTTPException(status_code=400, detail="El modo debe ser r o w")
        try:
            lock_id, expires = locks.acquire(path, body.mode, user["username"], min(body.ttl, 3600))
        except LockConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"lock_id": lock_id, "path": path, "mode": body.mode, "expires_in": round(expires - time.time())}

    @app.delete("/locks/{lock_id}", dependencies=deps)
    def unlock(lock_id: str, user: dict = Depends(current_user)):
        if not locks.release(lock_id, user["username"]):
            raise HTTPException(status_code=404, detail="Ese lock no existe o ya venció")
        return {"released": lock_id}

    # Transferencia de archivos (RF2): plan de escritura, commit y plan de lectura

    @app.post("/files/plan-write", dependencies=deps)
    def plan_write(body: PlanWriteIn, user: dict = Depends(current_user)):
        path = clean(body.path)
        if not locks.holds(body.lock_id, path, "w", user["username"]):
            raise HTTPException(status_code=409, detail="Necesita un lock de escritura vigente sobre el archivo")
        if body.size < 0:
            raise HTTPException(status_code=400, detail="Tamaño inválido")
        n_blocks = math.ceil(body.size / settings.block_size)
        try:
            placement = cluster.place_blocks(n_blocks)
        except NoDataNodes:
            raise HTTPException(status_code=503, detail="No hay DataNodes disponibles")
        upload_id = uuid.uuid4().hex
        blocks = []
        for idx, targets in enumerate(placement):
            block_id = uuid.uuid4().hex
            blocks.append({"idx": idx, "block_id": block_id, "targets": targets,
                           "token": block_token(block_id, "w")})
        cluster.register_upload(upload_id, {"path": path, "user": user["username"],
                                            "block_ids": [b["block_id"] for b in blocks]})
        return {"upload_id": upload_id, "block_size": settings.block_size,
                "min_replicas": min(settings.cluster.min_replicas, len(placement[0]) if placement else 1),
                "blocks": blocks}

    @app.post("/files/commit", dependencies=deps)
    def commit(body: CommitIn, user: dict = Depends(current_user)):
        upload = cluster.take_upload(body.upload_id)
        if not upload or upload["user"] != user["username"]:
            raise HTTPException(status_code=404, detail="La subida no existe o ya venció")
        path = upload["path"]
        if not locks.holds(body.lock_id, path, "w", user["username"]):
            raise HTTPException(status_code=409, detail="El lock de escritura venció antes del commit")
        received = sorted(body.blocks, key=lambda b: b.idx)
        if [b.block_id for b in received] != upload["block_ids"]:
            raise HTTPException(status_code=400, detail="Los bloques no coinciden con el plan de escritura")
        for b in received:
            cluster.record_stored(b.block_id, b.stored)
        existing = db.get_inode(path)
        submit({
            "type": "commit_file", "path": path,
            "owner": existing["owner"] if existing else user["username"],
            "grp": existing["grp"] if existing else db.get_inode(paths.parent(path))["grp"],
            "mode": existing["mode"] if existing else 0o640,
            "size": sum(b.size for b in received), "mtime": time.time(),
            "blocks": [{"idx": b.idx, "block_id": b.block_id, "size": b.size, "sha256": b.sha256}
                       for b in received],
        })
        return {"path": path, "size": sum(b.size for b in received), "blocks": len(received)}

    @app.get("/files/plan-read", dependencies=deps)
    def plan_read(path: str, lock_id: str, user: dict = Depends(current_user)):
        path = clean(path)
        inode = need_inode(path, "file")
        need_perm(user, inode, "r")
        if not locks.holds(lock_id, path, "r", user["username"]):
            raise HTTPException(status_code=409, detail="Necesita un lock de lectura vigente sobre el archivo")
        blocks = []
        for b in db.file_blocks(path):
            locations = cluster.locations(b["block_id"])
            random.shuffle(locations)  # reparte las lecturas entre las réplicas
            blocks.append({**b, "locations": locations, "token": block_token(b["block_id"], "r")})
        return {"path": path, "size": inode["size"], "version": inode["version"],
                "block_size": settings.block_size, "blocks": blocks}

    # Estado del clúster

    @app.get("/status")
    def status():
        return cluster.status()

    @app.get("/cluster", dependencies=deps)
    def cluster_info(user: dict = Depends(current_user)):
        info = cluster.summary()
        info["controlnodes"] = [cluster.status()]
        if settings.cluster.peer_url:
            peer = cluster._peer_status()
            info["controlnodes"].append(peer or {"node_id": "peer", "role": "caído",
                                                 "url": settings.cluster.peer_url})
        info["files"] = db.count_files()
        info["blocks"] = len(db.all_block_ids())
        info["active_locks"] = locks.count()
        info["block_size"] = settings.block_size
        return info

    # API interna entre nodos

    @app.post("/internal/heartbeat", dependencies=[Depends(internal)])
    def heartbeat(body: HeartbeatIn):
        return cluster.on_heartbeat(body.model_dump())

    @app.post("/internal/replicate", dependencies=[Depends(internal)])
    def replicate(body: ReplicateIn):
        return cluster.on_replicate(body.epoch, body.seq, body.op, body.leader_url, body.node_id)

    @app.get("/internal/log", dependencies=[Depends(internal)])
    def oplog(since: int = 0):
        return {"epoch": cluster.epoch, "entries": db.log_since(since)}

    return app
