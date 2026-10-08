"""Mide tiempos de subida y descarga según el tamaño del archivo y el paralelismo.

Sirve para la sección de rendimiento del informe: compara subir/bajar bloques
de a uno (workers=1) contra en paralelo (workers=4).

Uso:
    python scripts/benchmark.py --control https://IP1:8000,https://IP2:8000 --ca certs/ca.crt \
        --usuario admin --clave XXXX
Deja la tabla en resultados/benchmark.csv y la imprime en formato Markdown.
"""

import argparse
import csv
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dfsha.client.sdk import DFSClient  # noqa: E402

MB = 1024 * 1024


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--control", default=os.environ.get("DFSHA_CONTROL", "https://localhost:8001,https://localhost:8002"))
    parser.add_argument("--ca", default=os.environ.get("DFSHA_CA", str(ROOT / "certs" / "ca.crt")))
    parser.add_argument("--usuario", default="admin")
    parser.add_argument("--clave", required=True)
    parser.add_argument("--tamanos", default="1,10,50,100", help="tamaños en MB separados por coma")
    parser.add_argument("--workers", default="1,4", help="niveles de paralelismo a comparar")
    parser.add_argument("--repeticiones", type=int, default=3)
    parser.add_argument("--salida", default=str(ROOT / "resultados" / "benchmark.csv"))
    args = parser.parse_args()

    sizes = [int(x) for x in args.tamanos.split(",")]
    workers_list = [int(x) for x in args.workers.split(",")]
    folder = f"/bench_{int(time.time())}" if args.usuario == "admin" else f"/users/{args.usuario}/bench_{int(time.time())}"

    base = DFSClient(args.control.split(","), args.ca)
    base.login(args.usuario, args.clave)
    base.mkdir(folder)

    rows = []
    for size_mb in sizes:
        data = os.urandom(size_mb * MB)
        for workers in workers_list:
            client = DFSClient(args.control.split(","), args.ca, token=base.token, workers=workers)
            put_times, get_times = [], []
            for rep in range(args.repeticiones):
                path = f"{folder}/f{size_mb}mb_w{workers}_{rep}.bin"
                start = time.perf_counter()
                client.put_bytes(data, path)
                put_times.append(time.perf_counter() - start)
                start = time.perf_counter()
                back = client.get_bytes(path)
                get_times.append(time.perf_counter() - start)
                assert back == data, "el archivo descargado no coincide"
                client.rm(path)
            put_s, get_s = statistics.median(put_times), statistics.median(get_times)
            rows.append({"tamano_mb": size_mb, "workers": workers,
                         "put_s": round(put_s, 3), "put_mb_s": round(size_mb / put_s, 2),
                         "get_s": round(get_s, 3), "get_mb_s": round(size_mb / get_s, 2)})
            print(f"{size_mb:>4} MB  workers={workers}  put {put_s:.2f} s ({size_mb / put_s:.1f} MB/s)  "
                  f"get {get_s:.2f} s ({size_mb / get_s:.1f} MB/s)")
            client.close()
    base.rmdir(folder)

    out = Path(args.salida)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print("\n| Tamaño (MB) | Workers | put (s) | put (MB/s) | get (s) | get (MB/s) |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['tamano_mb']} | {r['workers']} | {r['put_s']} | {r['put_mb_s']} | {r['get_s']} | {r['get_mb_s']} |")
    print(f"\nResultados guardados en {out}")


if __name__ == "__main__":
    main()
