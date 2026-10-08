"""Fixtures de pytest: levantan un clúster local (2 ControlNodes y 4 DataNodes)
una sola vez para todas las pruebas, con tiempos de heartbeat cortos y bloques
de 1 MB para que todo sea rápido."""

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from cluster_local import LocalCluster  # noqa: E402

MB = 1024 * 1024
BLOCK = 1 * MB

USUARIOS = {
    "matias": ("clave_matias", ["equipo"]),
    "ana": ("clave_ana", ["equipo"]),
    "pedro": ("clave_pedro", []),
}


@pytest.fixture(scope="session")
def cluster(tmp_path_factory):
    c = LocalCluster(tmp_path_factory.mktemp("cluster"), datanodes=4, fast=True, block_size=BLOCK)
    c.start_all()
    try:
        c.wait_ready(90)
        yield c
    finally:
        c.stop_all()


@pytest.fixture(scope="session")
def admin(cluster):
    return cluster.admin()


@pytest.fixture(scope="session")
def usuarios(cluster, admin):
    clients = {}
    for name, (password, groups) in USUARIOS.items():
        admin.useradd(name, password, groups)
        client = cluster.client()
        client.login(name, password)
        clients[name] = client
    return clients


@pytest.fixture
def matias(usuarios):
    return usuarios["matias"]


@pytest.fixture
def carpeta(matias):
    """Carpeta nueva dentro del home de matias para cada prueba."""
    path = f"/users/matias/t_{uuid.uuid4().hex[:8]}"
    matias.mkdir(path)
    return path
