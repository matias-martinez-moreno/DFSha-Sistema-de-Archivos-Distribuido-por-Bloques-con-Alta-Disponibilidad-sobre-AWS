"""Genera los archivos de prueba en la carpeta pruebas/.

Los .bin tienen bytes aleatorios. El tamaño se escogió para ver distinto número
de bloques (con bloques de 4 MB): 1 MB = 1 bloque, 10 MB = 3, 50 MB = 13, 200 MB = 50.

Uso:
    python scripts/generar_archivos.py
    python scripts/generar_archivos.py --sin-grande   # omite el de 200 MB
"""

import argparse
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MB = 1024 * 1024

FILES = [
    ("pequeno_1mb.bin", 1 * MB),
    ("mediano_10mb.bin", 10 * MB),
    ("video_50mb.bin", 50 * MB),
    ("grande_200mb.bin", 200 * MB),
]

NOTAS = """Notas de prueba de DFSha
Este archivo se usa para mostrar open, read, write, close y lock.
Linea 3: el sistema parte los archivos en bloques y los replica en varios DataNodes.
Linea 4: si un DataNode se cae, el archivo se sigue pudiendo leer desde otra replica.
Linea 5: fin del archivo.
"""


def write_random(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    with open(path, "wb") as f:
        left = size
        while left:
            chunk = os.urandom(min(MB, left))
            f.write(chunk)
            digest.update(chunk)
            left -= len(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--salida", default=str(ROOT / "pruebas"))
    parser.add_argument("--sin-grande", action="store_true")
    args = parser.parse_args()

    out = Path(args.salida)
    out.mkdir(parents=True, exist_ok=True)
    (out / "notas.txt").write_text(NOTAS, encoding="utf-8")
    print(f"{'notas.txt':<20} {len(NOTAS.encode()):>12} bytes")
    for name, size in FILES:
        if args.sin_grande and size > 100 * MB:
            continue
        sha = write_random(out / name, size)
        print(f"{name:<20} {size:>12} bytes  sha256 {sha[:16]}...")
    print(f"Archivos en {out}. Agregue también una foto o un PDF propio para mostrar que se abre bien después del get.")


if __name__ == "__main__":
    main()
