"""Coordinación del clúster desde el ControlNode.

Aquí vive la lógica distribuida:
  1. Roles líder/seguidor entre los dos ControlNodes y el failover.
  2. Replicación síncrona del log de metadatos del líder al seguidor.
  3. Registro de DataNodes por heartbeat y mapa de ubicación de bloques.
  4. Ubicación de bloques nuevos por round robin.
  5. Monitor que detecta bloques con pocas réplicas y ordena re-replicarlos.
"""

import logging
import threading
import time

import httpx

from dfsha.common import config, security, tls
from dfsha.controlnode.db import MetadataDB

log = logging.getLogger("controlnode.cluster")


class NotLeader(Exception):
    pass


class NoDataNodes(Exception):
    pass


class ClusterSettings:
    def __init__(self):
        self.node_id = config.env("NODE_ID", "controlnode1")
        self.internal_url = config.required("INTERNAL_URL")
        self.peer_url = config.env("PEER_URL")
        self.internal_key = config.required("INTERNAL_KEY")
        self.ca_file = config.env("TLS_CA")
        self.admin_password = config.required("ADMIN_PASSWORD")
        self.replication_factor = config.env_int("REPLICATION_FACTOR", 3)
        self.min_replicas = config.env_int("MIN_REPLICAS", 2)
        self.dn_dead_after = config.env_float("DN_DEAD_AFTER", 10)
        self.role_interval = config.env_float("ROLE_INTERVAL", 2)
        self.failover_misses = config.env_int("FAILOVER_MISSES", 3)
        self.monitor_interval = config.env_float("MONITOR_INTERVAL", 5)
        self.pending_ttl = config.env_float("PENDING_TTL", 900)


class Cluster:
    def __init__(self, settings: ClusterSettings, db: MetadataDB):
        self.s = settings
        self.db = db
        self.http = httpx.Client(verify=tls.client_ssl_context(settings.ca_file), timeout=5)
        self.headers = {"X-Internal-Key": settings.internal_key}

        # Estado del rol
        self.role = "follower"
        self.leader_url: str | None = None
        self.epoch = int(db.get_meta("epoch", "0"))
        self.synced = False
        self.misses = 0
        self.peer_alive = False
        self.leader_since = 0.0
        self.submit_lock = threading.Lock()
        self.follow_lock = threading.RLock()

        # Estado de los DataNodes (solo en memoria, se reconstruye con heartbeats)
        self.dn_lock = threading.Lock()
        self.datanodes: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.inflight: dict[str, float] = {}
        self.uploads: dict[str, dict] = {}
        self.rr_counter = 0
        self.under_replicated = 0
        self.lost_blocks = 0

        self.stop = threading.Event()

    def start(self) -> None:
        if not self.s.peer_url:
            self._become_leader("no hay otro ControlNode configurado")
        threading.Thread(target=self._loop, args=(self._check_peer, self.s.role_interval), daemon=True).start()
        threading.Thread(target=self._loop, args=(self._monitor, self.s.monitor_interval), daemon=True).start()

    def _loop(self, fn, interval: float) -> None:
        while not self.stop.is_set():
            try:
                fn()
            except Exception:
                log.exception("Error en el ciclo %s", fn.__name__)
            self.stop.wait(interval)

    def status(self) -> dict:
        return {
            "node_id": self.s.node_id,
            "role": self.role,
            "epoch": self.epoch,
            "last_seq": self.db.last_seq(),
            "leader_url": self.leader_url,
            "peer_alive": self.peer_alive,
            "synced": self.synced,
        }

    # 1. Roles y failover

    def _check_peer(self) -> None:
        if not self.s.peer_url:
            return
        peer = self._peer_status()
        self.peer_alive = peer is not None

        if self.role == "leader":
            # Si la red se partió y volvió, puede haber dos líderes: se queda el de mayor epoch
            if peer and peer["role"] == "leader" and self._peer_wins(peer):
                self._step_down("el otro ControlNode es líder con mayor prioridad")
            return

        if peer and peer["role"] == "leader":
            self.misses = 0
            self.leader_url = self.s.peer_url
            if not self.synced:
                self._resync()
            elif peer["last_seq"] > self.db.last_seq():
                self._catch_up()
        elif peer and peer["role"] == "follower":
            # Arrancaron los dos al tiempo: lidera el que tenga datos más nuevos
            self.misses = 0
            if not self._peer_wins(peer):
                self._become_leader("los dos estaban como seguidores")
        else:
            self.misses += 1
            if self.misses >= self.s.failover_misses:
                self._become_leader(f"el líder no respondió {self.misses} veces seguidas")

    def _peer_wins(self, peer: dict) -> bool:
        mine = (self.epoch, self.db.last_seq(), _neg(self.s.node_id))
        theirs = (peer["epoch"], peer["last_seq"], _neg(peer["node_id"]))
        return theirs > mine

    def _peer_status(self) -> dict | None:
        try:
            r = self.http.get(f"{self.s.peer_url}/status", timeout=2)
            return r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    def _become_leader(self, reason: str) -> None:
        with self.submit_lock:
            self.epoch += 1
            self.db.set_meta("epoch", str(self.epoch))
            self.role = "leader"
            self.leader_url = self.s.internal_url
            self.leader_since = time.time()
            self.misses = 0
            self.synced = True
        log.warning("%s ahora es LÍDER (epoch %d): %s", self.s.node_id, self.epoch, reason)
        if self.db.count_users() == 0:
            self.submit({"type": "bootstrap", "admin_hash": security.hash_password(self.s.admin_password),
                         "mtime": time.time()})

    def _step_down(self, reason: str) -> None:
        self.role = "follower"
        self.synced = False
        self.leader_url = None
        self.misses = 0
        log.warning("%s deja de ser líder: %s", self.s.node_id, reason)

    # 2. Replicación del log de metadatos

    def submit(self, op: dict) -> int:
        """Aplica una operación en el líder y la replica de forma síncrona al seguidor."""
        with self.submit_lock:
            if self.role != "leader":
                raise NotLeader()
            seq = self.db.last_seq() + 1
            self.db.append_and_apply(seq, op)
            if self.s.peer_url and self.peer_alive:
                try:
                    r = self.http.post(f"{self.s.peer_url}/internal/replicate", headers=self.headers, json={
                        "epoch": self.epoch, "seq": seq, "op": op,
                        "leader_url": self.s.internal_url, "node_id": self.s.node_id,
                    })
                    if r.json().get("stale_leader"):
                        self._step_down("el seguidor tiene un epoch mayor")
                        raise NotLeader()
                except httpx.HTTPError as exc:
                    # Si el seguidor no responde, el líder sigue solo. Cuando el
                    # seguidor vuelva se pone al día leyendo el log.
                    log.warning("No se pudo replicar la operación %d al seguidor: %s", seq, exc)
                    self.peer_alive = False
            return seq

    def on_replicate(self, epoch: int, seq: int, op: dict, leader_url: str, node_id: str) -> dict:
        with self.follow_lock:
            if epoch < self.epoch or (self.role == "leader" and epoch == self.epoch
                                      and _neg(node_id) < _neg(self.s.node_id)):
                return {"ok": False, "stale_leader": True, "epoch": self.epoch}
            if self.role == "leader":
                self._step_down("llegó una operación de un líder más nuevo")
            if epoch != self.epoch:
                self.epoch = epoch
                self.db.set_meta("epoch", str(epoch))
            self.leader_url = leader_url
            self.misses = 0
            if not self.synced:
                self._resync()
                return {"ok": True}
            last = self.db.last_seq()
            if seq <= last:
                return {"ok": True}
            if seq > last + 1:
                self._catch_up()
                if self.db.last_seq() >= seq:
                    return {"ok": True}
            self.db.append_and_apply(seq, op)
            return {"ok": True}

    def _fetch_log(self, since: int) -> dict | None:
        source = self.leader_url or self.s.peer_url
        try:
            r = self.http.get(f"{source}/internal/log", params={"since": since}, headers=self.headers, timeout=30)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            log.warning("No se pudo leer el log del líder: %s", exc)
            return None

    def _resync(self) -> None:
        """Copia completa: borra los metadatos locales y repite todo el log del líder."""
        with self.follow_lock:
            data = self._fetch_log(0)
            if data is None:
                return
            self.db.reset_and_load([(seq, op) for seq, op in data["entries"]])
            self.epoch = data["epoch"]
            self.db.set_meta("epoch", str(self.epoch))
            self.synced = True
            log.info("Seguidor sincronizado con el líder (%d operaciones)", len(data["entries"]))

    def _catch_up(self) -> None:
        """Copia incremental: pide solo las operaciones que le faltan."""
        with self.follow_lock:
            data = self._fetch_log(self.db.last_seq())
            if data is None:
                return
            for seq, op in data["entries"]:
                if seq == self.db.last_seq() + 1:
                    self.db.append_and_apply(seq, op)

    # 3. DataNodes y ubicación de bloques

    def on_heartbeat(self, hb: dict) -> dict:
        blocks = set(hb["blocks"])
        with self.dn_lock:
            known = self.datanodes.get(hb["node_id"])
            if known is None or time.time() - known["last_seen"] > self.s.dn_dead_after:
                log.info("DataNode %s activo (%d bloques)", hb["node_id"], len(blocks))
            self.datanodes[hb["node_id"]] = {
                "public_url": hb["public_url"],
                "internal_url": hb["internal_url"],
                "used_bytes": hb["used_bytes"],
                "blocks": blocks,
                "last_seen": time.time(),
            }
            if self.role != "leader":
                return {"is_leader": False, "delete": [], "replicate": []}
            orders = self.commands.pop(hb["node_id"], {"replicate": []})
        to_delete = self.db.garbage_set() & blocks
        return {"is_leader": True, "delete": sorted(to_delete), "replicate": orders["replicate"]}

    def _live_ids(self) -> list[str]:
        now = time.time()
        return sorted(dn for dn, info in self.datanodes.items() if now - info["last_seen"] <= self.s.dn_dead_after)

    def live_datanodes(self) -> list[str]:
        with self.dn_lock:
            return self._live_ids()

    def _dn_ref(self, dn_id: str) -> dict:
        info = self.datanodes[dn_id]
        return {"id": dn_id, "url": info["public_url"], "internal_url": info["internal_url"]}

    def place_blocks(self, n_blocks: int) -> list[list[dict]]:
        """Round robin: el bloque i empieza en el siguiente DataNode de la lista y
        sus réplicas van en los nodos que siguen. Así la carga queda pareja."""
        with self.dn_lock:
            live = self._live_ids()
            if not live:
                raise NoDataNodes()
            copies = min(self.s.replication_factor, len(live))
            plan = []
            for _ in range(n_blocks):
                start = self.rr_counter % len(live)
                self.rr_counter += 1
                plan.append([self._dn_ref(live[(start + j) % len(live)]) for j in range(copies)])
            return plan

    def record_stored(self, block_id: str, dn_ids: list[str]) -> None:
        """Anota de una vez dónde quedó un bloque recién escrito, sin esperar al heartbeat."""
        with self.dn_lock:
            for dn in dn_ids:
                if dn in self.datanodes:
                    self.datanodes[dn]["blocks"].add(block_id)

    def locations(self, block_id: str) -> list[dict]:
        with self.dn_lock:
            return [self._dn_ref(dn) for dn in self._live_ids() if block_id in self.datanodes[dn]["blocks"]]

    def register_upload(self, upload_id: str, info: dict) -> None:
        with self.dn_lock:
            info["created"] = time.time()
            self.uploads[upload_id] = info

    def take_upload(self, upload_id: str) -> dict | None:
        with self.dn_lock:
            return self.uploads.pop(upload_id, None)

    # 4. Monitor de réplicas

    def _monitor(self) -> None:
        if self.role != "leader":
            return
        self._expire_uploads()
        # Recién elegido, el líder espera a que todos los DataNodes reporten antes de decidir
        if time.time() - self.leader_since < self.s.dn_dead_after * 2:
            return
        self._check_replication()

    def _check_replication(self) -> None:
        now = time.time()
        block_ids = self.db.all_block_ids()
        with self.dn_lock:
            live = self._live_ids()
            holders: dict[str, set[str]] = {}
            for dn in live:
                for bid in self.datanodes[dn]["blocks"]:
                    holders.setdefault(bid, set()).add(dn)
            wanted = min(self.s.replication_factor, len(live))
            under = lost = 0
            for bid in block_ids:
                have = holders.get(bid, set())
                if not have:
                    lost += 1
                    continue
                if len(have) >= wanted:
                    continue
                under += 1
                if now - self.inflight.get(bid, 0) < 15:
                    continue  # ya se pidió hace poco, se espera a que termine
                candidates = sorted((dn for dn in live if dn not in have),
                                    key=lambda dn: len(self.datanodes[dn]["blocks"]))
                source = sorted(have)[0]
                target = candidates[0]
                self.commands.setdefault(source, {"replicate": []})["replicate"].append(
                    {"block_id": bid, "target_url": self.datanodes[target]["internal_url"]})
                self.inflight[bid] = now
                log.info("Re-replicando bloque %s: %s -> %s", bid[:8], source, target)
            self.under_replicated = under
            self.lost_blocks = lost

    def _expire_uploads(self) -> None:
        """Subidas que nunca hicieron commit: sus bloques pasan a basura."""
        now = time.time()
        with self.dn_lock:
            expired = [uid for uid, u in self.uploads.items() if now - u["created"] > self.s.pending_ttl]
            block_ids = [bid for uid in expired for bid in self.uploads.pop(uid)["block_ids"]]
        if block_ids:
            self.submit({"type": "gc", "block_ids": block_ids, "mtime": now})

    def summary(self) -> dict:
        now = time.time()
        with self.dn_lock:
            datanodes = [{
                "id": dn,
                "url": info["public_url"],
                "alive": now - info["last_seen"] <= self.s.dn_dead_after,
                "blocks": len(info["blocks"]),
                "used_bytes": info["used_bytes"],
                "last_seen_seconds": round(now - info["last_seen"], 1),
            } for dn, info in sorted(self.datanodes.items())]
        return {
            "datanodes": datanodes,
            "replication_factor": self.s.replication_factor,
            "under_replicated": self.under_replicated,
            "lost_blocks": self.lost_blocks,
        }


def _neg(node_id: str) -> tuple:
    """Para desempatar: gana el id más pequeño (controlnode1 antes que controlnode2)."""
    return tuple(-ord(ch) for ch in node_id)
