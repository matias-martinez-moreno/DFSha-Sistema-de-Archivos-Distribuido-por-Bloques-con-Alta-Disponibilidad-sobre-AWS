"""P2. Particionamiento y distribución de bloques."""

import os
from collections import Counter

from conftest import BLOCK


def test_archivo_repartido_en_varios_datanodes(matias, carpeta, cluster):
    matias.put_bytes(os.urandom(10 * BLOCK), f"{carpeta}/grande.bin")
    info = matias.blocks(f"{carpeta}/grande.bin")
    assert len(info["blocks"]) == 10

    por_nodo = Counter()
    for b in info["blocks"]:
        # cada bloque tiene 3 réplicas en DataNodes distintos
        assert len(b["locations"]) == 3
        assert len(set(b["locations"])) == 3
        por_nodo.update(b["locations"])

    # todos los DataNodes guardan parte del archivo, pero ninguno lo tiene completo
    assert set(por_nodo) == set(cluster.datanodes)
    assert all(0 < n < 10 for n in por_nodo.values())


def test_round_robin_reparte_parejo(matias, carpeta):
    matias.put_bytes(os.urandom(8 * BLOCK), f"{carpeta}/rr.bin")
    info = matias.blocks(f"{carpeta}/rr.bin")
    por_nodo = Counter(loc for b in info["blocks"] for loc in b["locations"])
    # 8 bloques x 3 réplicas = 24 copias entre 4 nodos: 6 por nodo
    assert sorted(por_nodo.values()) == [6, 6, 6, 6]
