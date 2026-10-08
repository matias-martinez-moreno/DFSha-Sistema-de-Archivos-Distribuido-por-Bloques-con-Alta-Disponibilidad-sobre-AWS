"""Levanta el clúster completo como procesos locales, sin Docker.

Sirve para desarrollar y para las pruebas automáticas (pytest lo usa para
poder apagar y prender nodos). Para la demo se usa Docker Compose o AWS.

Uso:
    python scripts/cluster_local.py               # 2 ControlNodes y 4 DataNodes
    python scripts/cluster_local.py --datanodes 5
Ctrl+C apaga todo.
"""

import argparse
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from generar_seguridad import generate_certs, node_names  # noqa: E402

from dfsha.client.sdk import DFSClient, DFSError  # noqa: E402


class LocalCluster:
    def __init__(self, workdir: Path, datanodes: int = 4, fast: bool = False, block_size: int | None = None,
                 cn_base_port: int = 18001, dn_base_port: int = 18101):
        self.workdir = Path(workdir)
        self.certs = self.workdir / "certs"
        self.n_datanodes = datanodes
        self.admin_password = secrets.token_urlsafe(9)
        self.procs: dict[str, subprocess.Popen] = {}
        generate_certs(self.certs, node_names(datanodes))

        shared = {
            "JWT_SECRET": secrets.token_urlsafe(32),
            "INTERNAL_KEY": secrets.token_urlsafe(32),
            "BLOCK_TOKEN_SECRET": secrets.token_urlsafe(32),
            "ADMIN_PASSWORD": self.admin_password,
            "TLS_CA": str(self.certs / "ca.crt"),
            "INTERNAL_CIDRS": "127.0.0.1/32",
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        if fast:
            # Tiempos cortos para que las pruebas de fallos no tarden minutos
            shared.update(HEARTBEAT_INTERVAL="1", DN_DEAD_AFTER="4", ROLE_INTERVAL="1",
                          FAILOVER_MISSES="3", MONITOR_INTERVAL="1")
        if block_size:
            shared["BLOCK_SIZE"] = str(block_size)

        cn_urls = [f"https://127.0.0.1:{cn_base_port + i}" for i in range(2)]
        self.control_urls = cn_urls
        self.specs: dict[str, dict] = {}
        for i, name in enumerate(["controlnode1", "controlnode2"]):
            self.specs[name] = {"module": "dfsha.controlnode", "env": {
                **shared, "NODE_ID": name, "PORT": str(cn_base_port + i),
                "INTERNAL_URL": cn_urls[i], "PEER_URL": cn_urls[1 - i],
                "DB_PATH": str(self.workdir / name / "metadata.db"),
                "TLS_CERT": str(self.certs / f"{name}.crt"), "TLS_KEY": str(self.certs / f"{name}.key"),
            }}
        for i in range(datanodes):
            name = f"datanode{i + 1}"
            url = f"https://127.0.0.1:{dn_base_port + i}"
            self.specs[name] = {"module": "dfsha.datanode", "env": {
                **shared, "NODE_ID": name, "PORT": str(dn_base_port + i),
                "PUBLIC_URL": url, "INTERNAL_URL": url, "CONTROL_URLS": ",".join(cn_urls),
                "DATA_DIR": str(self.workdir / name), "STORAGE_KEY": secrets.token_urlsafe(32),
                "TLS_CERT": str(self.certs / f"{name}.crt"), "TLS_KEY": str(self.certs / f"{name}.key"),
            }}

    @property
    def datanodes(self) -> list[str]:
        return [n for n in self.specs if n.startswith("datanode")]

    def start(self, name: str) -> None:
        spec = self.specs[name]
        log_file = open(self.workdir / f"{name}.log", "a", encoding="utf-8")
        env = {**os.environ, **spec["env"]}
        self.procs[name] = subprocess.Popen([sys.executable, "-m", spec["module"]], env=env,
                                            stdout=log_file, stderr=subprocess.STDOUT)

    def stop(self, name: str) -> None:
        proc = self.procs.pop(name, None)
        if proc:
            proc.terminate()
            proc.wait(timeout=15)

    def start_all(self) -> None:
        for name in self.specs:
            self.start(name)

    def stop_all(self) -> None:
        for name in list(self.procs):
            self.stop(name)

    def client(self, **kwargs) -> DFSClient:
        return DFSClient(self.control_urls, str(self.certs / "ca.crt"), **kwargs)

    def admin(self) -> DFSClient:
        c = self.client()
        c.login("admin", self.admin_password)
        return c

    def leader_name(self) -> str:
        c = self.client()
        url = c.find_leader()
        c.close()
        return "controlnode1" if url == self.control_urls[0] else "controlnode2"

    def wait_ready(self, timeout: float = 60) -> None:
        """Espera a que haya líder, seguidor sincronizado y todos los DataNodes vivos."""
        deadline = time.time() + timeout
        last_error = None
        admin = None
        while time.time() < deadline:
            try:
                admin = admin or self.admin()
                info = admin.cluster()
                alive = sum(1 for dn in info["datanodes"] if dn["alive"])
                roles = sorted(cn.get("role") for cn in info["controlnodes"])
                synced = all(cn.get("synced") for cn in info["controlnodes"])
                seqs = {cn.get("last_seq") for cn in info["controlnodes"]}
                if alive == self.n_datanodes and roles == ["follower", "leader"] and synced and len(seqs) == 1:
                    admin.close()
                    return
                last_error = f"DataNodes vivos {alive}, roles {roles}, sincronizados {synced}"
            except DFSError as exc:
                admin = None
                last_error = str(exc)
            time.sleep(0.5)
        raise RuntimeError(f"El clúster no quedó listo a tiempo: {last_error}")

    def block_file(self, datanode: str, block_id: str) -> Path:
        return self.workdir / datanode / "blocks" / block_id[:2] / f"{block_id}.blk"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--datanodes", type=int, default=4)
    parser.add_argument("--dir", default=None, help="carpeta para datos y logs (por defecto una temporal)")
    args = parser.parse_args()
    workdir = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="dfsha-"))
    cluster = LocalCluster(workdir, datanodes=args.datanodes)
    cluster.start_all()
    try:
        cluster.wait_ready()
        print("Clúster local listo")
        print(f"  Datos y logs: {workdir}")
        print(f"  dfsha config {','.join(cluster.control_urls)} {cluster.certs / 'ca.crt'}")
        print(f"  dfsha login admin   (contraseña: {cluster.admin_password})")
        print("Ctrl+C para apagar")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cluster.stop_all()


if __name__ == "__main__":
    main()
