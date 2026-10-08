"""P3 y P4. Alta disponibilidad: caída de DataNodes y failover del ControlNode.

Estas pruebas apagan nodos de verdad (procesos) y al final los vuelven a prender.
"""

import os

from conftest import BLOCK
from helpers import wait_until


def replicas_completas(client, path, vivos: int) -> bool:
    info = client.blocks(path)
    return all(len(b["locations"]) >= min(3, vivos) for b in info["blocks"])


def test_caida_de_datanode_lectura_y_re_replicacion(matias, carpeta, cluster, admin):
    data = os.urandom(6 * BLOCK)
    path = f"{carpeta}/tolerante.bin"
    matias.put_bytes(data, path)

    cluster.stop("datanode2")
    try:
        # se lee de inmediato, aunque el ControlNode todavía crea que datanode2 está vivo
        assert matias.get_bytes(path) == data

        # el líder detecta la caída y vuelve a dejar 3 réplicas en los nodos vivos
        wait_until(lambda: not next(d for d in admin.cluster()["datanodes"] if d["id"] == "datanode2")["alive"],
                   timeout=15, message="que el ControlNode marque datanode2 como caído")
        wait_until(lambda: replicas_completas(matias, path, 3) and admin.cluster()["under_replicated"] == 0,
                   timeout=40, message="la re-replicación de los bloques")
        info = matias.blocks(path)
        assert all("datanode2" not in b["locations"] for b in info["blocks"])
        assert matias.get_bytes(path) == data
    finally:
        cluster.start("datanode2")
        cluster.wait_ready(60)


def test_escritura_con_un_datanode_caido(matias, carpeta, cluster, admin):
    cluster.stop("datanode3")
    try:
        wait_until(lambda: sum(d["alive"] for d in admin.cluster()["datanodes"]) == 3,
                   timeout=15, message="que el ControlNode vea 3 DataNodes vivos")
        data = os.urandom(3 * BLOCK)
        matias.put_bytes(data, f"{carpeta}/con_falla.bin")
        info = matias.blocks(f"{carpeta}/con_falla.bin")
        assert all(len(b["locations"]) == 3 and "datanode3" not in b["locations"] for b in info["blocks"])
        assert matias.get_bytes(f"{carpeta}/con_falla.bin") == data
    finally:
        cluster.start("datanode3")
        cluster.wait_ready(60)


def test_failover_del_controlnode(matias, carpeta, cluster):
    antes = os.urandom(2 * BLOCK)
    matias.put_bytes(antes, f"{carpeta}/antes.bin")
    viejo_lider = cluster.leader_name()
    otro = "controlnode2" if viejo_lider == "controlnode1" else "controlnode1"

    cluster.stop(viejo_lider)
    # el cliente es el mismo: el SDK busca solo al nuevo líder y reintenta
    assert [e["name"] for e in matias.ls(carpeta)] == ["antes.bin"]
    assert cluster.leader_name() == otro
    assert matias.get_bytes(f"{carpeta}/antes.bin") == antes
    durante = os.urandom(2 * BLOCK)
    matias.put_bytes(durante, f"{carpeta}/durante.bin")

    # el viejo líder vuelve como seguidor y se sincroniza copiando el log
    cluster.start(viejo_lider)
    cluster.wait_ready(60)
    assert cluster.leader_name() == otro

    # si ahora se cae el nuevo líder, el que volvió debe tener todo, incluido lo escrito durante la falla
    cluster.stop(otro)
    try:
        assert matias.get_bytes(f"{carpeta}/durante.bin") == durante
        assert cluster.leader_name() == viejo_lider
    finally:
        cluster.start(otro)
        cluster.wait_ready(60)
