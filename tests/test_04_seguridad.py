"""P7. Seguridad: autenticación, permisos, cifrado en disco y en tránsito, llamadas internas."""

import httpx
import pytest

from dfsha.client.sdk import DFSError
from dfsha.common import tls


@pytest.fixture
def http(cluster):
    return httpx.Client(verify=tls.client_ssl_context(str(cluster.certs / "ca.crt")))


def leader_url(cluster):
    c = cluster.client()
    return c.find_leader()


def test_sin_token_responde_401(cluster, http):
    r = http.get(f"{leader_url(cluster)}/fs/ls", params={"path": "/"})
    assert r.status_code == 401


def test_clave_incorrecta(cluster):
    c = cluster.client()
    with pytest.raises(DFSError) as err:
        c.login("matias", "clave_mala")
    assert err.value.status == 401


def test_permisos_por_usuario_y_grupo(usuarios, carpeta):
    matias, ana, pedro = usuarios["matias"], usuarios["ana"], usuarios["pedro"]
    path = f"{carpeta}/privado.txt"
    matias.put_bytes(b"solo para el equipo", path)

    # por defecto el archivo es rw-r----- del grupo matias: ni ana ni pedro lo leen
    for otro in (ana, pedro):
        with pytest.raises(DFSError) as err:
            otro.get_bytes(path)
        assert err.value.status == 403

    # matias lo comparte con el grupo equipo: ana entra, pedro sigue afuera
    matias.chgrp(path, "equipo")
    assert ana.get_bytes(path) == b"solo para el equipo"
    with pytest.raises(DFSError) as err:
        pedro.get_bytes(path)
    assert err.value.status == 403


def test_no_puede_escribir_en_home_ajeno(usuarios):
    with pytest.raises(DFSError) as err:
        usuarios["pedro"].put_bytes(b"intruso", "/users/matias/intruso.txt")
    assert err.value.status == 403


def test_solo_admin_crea_usuarios(usuarios):
    with pytest.raises(DFSError) as err:
        usuarios["pedro"].useradd("nuevo", "clave123")
    assert err.value.status == 403


def test_bloques_cifrados_en_disco(matias, carpeta, cluster):
    secreto = b"TEXTO-SECRETO-DFSHA " * 200
    matias.put_bytes(secreto, f"{carpeta}/secreto.txt")
    block = matias.blocks(f"{carpeta}/secreto.txt")["blocks"][0]
    for dn in block["locations"]:
        en_disco = cluster.block_file(dn, block["block_id"]).read_bytes()
        assert b"TEXTO-SECRETO" not in en_disco
        assert len(en_disco) == len(secreto) + 12 + 16  # nonce + etiqueta de AES-GCM


def test_datanode_exige_permiso_de_bloque(matias, carpeta, http):
    matias.put_bytes(b"a" * 10, f"{carpeta}/a.txt")
    matias.put_bytes(b"b" * 10, f"{carpeta}/b.txt")
    lock_a = matias.lock(f"{carpeta}/a.txt", "r")
    plan_a = matias._read_plan(f"{carpeta}/a.txt", lock_a)
    lock_b = matias.lock(f"{carpeta}/b.txt", "r")
    plan_b = matias._read_plan(f"{carpeta}/b.txt", lock_b)
    block_a, block_b = plan_a["blocks"][0], plan_b["blocks"][0]
    url = block_a["locations"][0]["url"]

    assert http.get(f"{url}/blocks/{block_a['block_id']}").status_code == 401
    r = http.get(f"{url}/blocks/{block_a['block_id']}", headers={"Authorization": f"Bearer {block_b['token']}"})
    assert r.status_code == 403
    r = http.get(f"{url}/blocks/{block_a['block_id']}", headers={"Authorization": f"Bearer {block_a['token']}"})
    assert r.status_code == 200 and r.content == b"a" * 10
    matias.unlock(lock_a)
    matias.unlock(lock_b)


def test_endpoints_internos_requieren_llave(cluster, http):
    r = http.post(f"{leader_url(cluster)}/internal/heartbeat", json={})
    assert r.status_code == 403
    r = http.get(f"{leader_url(cluster)}/internal/log", headers={"X-Internal-Key": "adivinando"})
    assert r.status_code == 403


def test_solo_acepta_tls_con_nuestra_ca(cluster):
    https_url = cluster.control_urls[0]
    # sin TLS no hay conexión
    with pytest.raises(httpx.HTTPError):
        httpx.get(https_url.replace("https://", "http://") + "/status", timeout=3)
    # un cliente que no confía en nuestra CA rechaza el certificado del servidor
    with pytest.raises(httpx.ConnectError):
        httpx.get(https_url + "/status", timeout=3)
