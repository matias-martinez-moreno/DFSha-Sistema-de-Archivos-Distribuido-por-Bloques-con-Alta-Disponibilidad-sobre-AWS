"""P1. Requisitos funcionales: gestión (RF1), transferencia (RF2) y acceso (RF3)."""

import os

import pytest
from conftest import BLOCK, MB
from helpers import sha256_file

from dfsha.client import cli
from dfsha.client.sdk import DFSError


def nombres(entries):
    return sorted(e["name"] for e in entries)


def test_mkdir_ls_rmdir(matias, carpeta):
    matias.mkdir(f"{carpeta}/docs")
    matias.mkdir(f"{carpeta}/fotos")
    assert nombres(matias.ls(carpeta)) == ["docs", "fotos"]
    matias.rmdir(f"{carpeta}/fotos")
    assert nombres(matias.ls(carpeta)) == ["docs"]


def test_rmdir_no_vacio_falla(matias, carpeta):
    matias.mkdir(f"{carpeta}/a")
    matias.put_bytes(b"hola", f"{carpeta}/a/x.txt")
    with pytest.raises(DFSError) as err:
        matias.rmdir(f"{carpeta}/a")
    assert err.value.status == 409


def test_rm_borra_archivo(matias, carpeta):
    matias.put_bytes(b"contenido", f"{carpeta}/borrar.txt")
    matias.rm(f"{carpeta}/borrar.txt")
    assert matias.ls(carpeta) == []
    assert not matias.exists(f"{carpeta}/borrar.txt")


@pytest.mark.parametrize("size", [0, 1, BLOCK, BLOCK + 1, int(3.5 * BLOCK), 10 * MB])
def test_put_get_integridad(matias, carpeta, tmp_path, size):
    original = tmp_path / "original.bin"
    original.write_bytes(os.urandom(size))
    remoto = f"{carpeta}/archivo_{size}.bin"
    resultado = matias.put(str(original), remoto)
    assert resultado["blocks"] == -(-size // BLOCK)  # techo de size / BLOCK

    copia = tmp_path / "copia.bin"
    matias.get(remoto, str(copia))
    assert sha256_file(copia) == sha256_file(original)
    assert matias.stat(remoto)["size"] == size


def test_open_read_por_rangos(matias, carpeta):
    data = os.urandom(3 * BLOCK + 500)
    matias.put_bytes(data, f"{carpeta}/rangos.bin")
    with matias.open(f"{carpeta}/rangos.bin", "r") as f:
        assert f.size == len(data)
        # un rango que cruza la frontera entre el bloque 0 y el 1
        assert f.pread(BLOCK - 100, 200) == data[BLOCK - 100:BLOCK + 100]
        # solo debió descargar los bloques 0 y 1
        assert sorted(f._cache) == [0, 1]
        f.seek(3 * BLOCK)
        assert f.read() == data[3 * BLOCK:]


def test_open_write_close(matias, carpeta):
    path = f"{carpeta}/nuevo.txt"
    with matias.open(path, "w") as f:
        f.write("primera linea\n")
        f.write("segunda linea\n")
    assert matias.get_bytes(path) == b"primera linea\nsegunda linea\n"
    with matias.open(path, "w") as f:
        f.write("version 2\n")
    assert matias.get_bytes(path) == b"version 2\n"
    assert matias.stat(path)["version"] == 2


def test_cli_basico(cluster, usuarios, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DFSHA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DFSHA_CONTROL", ",".join(cluster.control_urls))
    monkeypatch.setenv("DFSHA_CA", str(cluster.certs / "ca.crt"))
    local = tmp_path / "cli.txt"
    local.write_text("archivo subido desde la CLI\n")

    def correr(*argv):
        assert cli.run(cli.Session(), list(argv)) == 0
        return capsys.readouterr().out

    assert "Sesión iniciada como pedro" in correr("login", "pedro", "clave_pedro")
    correr("mkdir", "cli")
    correr("cd", "cli")
    assert correr("pwd").strip() == "/users/pedro/cli"
    assert "OK" in correr("put", str(local))
    assert "cli.txt" in correr("ls")
    assert "archivo subido desde la CLI" in correr("cat", "cli.txt")
    assert "bloque 0" in correr("info", "cli.txt")
    correr("get", "cli.txt", str(tmp_path / "bajado.txt"))
    assert (tmp_path / "bajado.txt").read_text() == local.read_text()
