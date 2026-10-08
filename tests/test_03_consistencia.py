"""P5 y P6. Consistencia y concurrencia: locks, commit atómico y versiones."""

import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from conftest import BLOCK
from helpers import wait_until

from dfsha.client.sdk import DFSError
from dfsha.common import tls


@pytest.fixture
def compartida(usuarios, carpeta):
    """Carpeta de matias compartida con el grupo equipo (ana sí, pedro no)."""
    matias = usuarios["matias"]
    matias.chgrp(carpeta, "equipo")
    matias.chmod(carpeta, "770")
    return carpeta


def test_dos_escritores_mismo_archivo(usuarios, compartida):
    matias, ana = usuarios["matias"], usuarios["ana"]
    path = f"{compartida}/informe.txt"
    lock_id = matias.lock(path, "w")
    with pytest.raises(DFSError) as err:
        ana.put_bytes(b"cambio de ana", path)
    assert err.value.status == 409
    matias.unlock(lock_id)
    ana.put_bytes(b"cambio de ana", path)  # ahora sí puede


def test_lector_bloquea_al_escritor(usuarios, compartida):
    matias, ana = usuarios["matias"], usuarios["ana"]
    path = f"{compartida}/datos.bin"
    matias.put_bytes(b"v1" * 1000, path)
    matias.chmod(path, "660")
    with ana.open(path, "r") as f:
        with pytest.raises(DFSError) as err:
            matias.put_bytes(b"v2" * 1000, path)
        assert err.value.status == 409
        assert f.read() == b"v1" * 1000
    matias.put_bytes(b"v2" * 1000, path)
    assert ana.get_bytes(path) == b"v2" * 1000


def test_varios_lectores_a_la_vez(usuarios, compartida):
    matias, ana = usuarios["matias"], usuarios["ana"]
    path = f"{compartida}/compartido.txt"
    matias.put_bytes(b"lectura compartida", path)
    matias.chmod(path, "640")
    with matias.open(path, "r") as a, ana.open(path, "r") as b:
        assert a.read() == b.read() == b"lectura compartida"


def test_subida_sin_commit_no_es_visible(matias, carpeta):
    path = f"{carpeta}/incompleto.bin"
    lock_id = matias.lock(path, "w")
    plan = matias._request("POST", "/files/plan-write", json={"path": path, "size": 3 * BLOCK, "lock_id": lock_id})
    # se sube solo el primer bloque y el cliente "se cae" antes del commit
    first = plan["blocks"][0]
    matias._send_block(first, os.urandom(BLOCK), plan["min_replicas"])
    matias.unlock(lock_id)
    assert matias.ls(carpeta) == []
    assert not matias.exists(path)


def test_sobrescribir_crea_version_y_borra_bloques_viejos(matias, carpeta, cluster):
    path = f"{carpeta}/versionado.bin"
    matias.put_bytes(os.urandom(2 * BLOCK), path)
    viejos = matias.blocks(path)["blocks"]
    nuevo = os.urandom(2 * BLOCK)
    matias.put_bytes(nuevo, path)
    assert matias.get_bytes(path) == nuevo
    assert matias.stat(path)["version"] == 2

    def viejos_borrados():
        return not any(cluster.block_file(dn, b["block_id"]).exists()
                       for b in viejos for dn in cluster.datanodes)
    wait_until(viejos_borrados, timeout=20, message="el borrado de los bloques de la versión anterior")


def test_escrituras_concurrentes_de_archivos_distintos(matias, carpeta):
    datos = {f"{carpeta}/c{i}.bin": os.urandom(BLOCK + i) for i in range(6)}
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda item: matias.put_bytes(item[1], item[0]), datos.items()))
    for path, data in datos.items():
        assert matias.get_bytes(path) == data


def test_metadatos_replicados_en_el_seguidor(matias, carpeta, cluster):
    matias.put_bytes(b"replicado", f"{carpeta}/r.txt")
    http = httpx.Client(verify=tls.client_ssl_context(str(cluster.certs / "ca.crt")))

    def iguales():
        estados = [http.get(f"{url}/status").json() for url in cluster.control_urls]
        return estados[0]["last_seq"] == estados[1]["last_seq"]
    wait_until(iguales, timeout=10, message="que el seguidor tenga el mismo log que el líder")
