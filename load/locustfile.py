"""Prueba de carga de DFSha con Locust (P8 y P9).

Cada usuario simulado usa el mismo SDK que la CLI: hace login, sube archivos
pequeños, los descarga y lista su carpeta. Locust mide tiempos y errores de
cada operación como si fueran peticiones.

Variables de entorno:
    DFSHA_CONTROL         URLs de los ControlNodes separadas por coma
    DFSHA_CA              ruta de certs/ca.crt
    DFSHA_ADMIN_PASSWORD  contraseña del admin (para crear el usuario de carga)
    DFSHA_WAIT_MIN y DFSHA_WAIT_MAX  pausa entre operaciones de cada usuario (0.5 y 2 por defecto)

Ejemplo con interfaz web (http://localhost:8089):
    locust -f load/locustfile.py
Ejemplo sin interfaz, 50 usuarios por 2 minutos, guardando CSV:
    locust -f load/locustfile.py --headless -u 50 -r 5 -t 2m --csv resultados/carga_50
"""

import os
import random
import time
import uuid

from locust import User, between, events, task

from dfsha.client.sdk import DFSClient, DFSError

CONTROL = os.environ.get("DFSHA_CONTROL", "https://localhost:8001,https://localhost:8002").split(",")
CA = os.environ.get("DFSHA_CA", "certs/ca.crt")
LOAD_USER = os.environ.get("DFSHA_LOAD_USER", "carga")
LOAD_PASSWORD = os.environ.get("DFSHA_LOAD_PASSWORD", "carga_dfsha")
SIZES = [64 * 1024, 512 * 1024, 2 * 1024 * 1024]


@events.test_start.add_listener
def crear_usuario_de_carga(environment, **_kwargs):
    admin_password = os.environ.get("DFSHA_ADMIN_PASSWORD")
    if not admin_password:
        return
    admin = DFSClient(CONTROL, CA)
    admin.login("admin", admin_password)
    try:
        admin.useradd(LOAD_USER, LOAD_PASSWORD)
        print(f"Usuario {LOAD_USER} creado para la prueba de carga")
    except DFSError as exc:
        if exc.status != 409:
            raise
    finally:
        admin.close()


class DFSUser(User):
    wait_time = between(float(os.environ.get("DFSHA_WAIT_MIN", 0.5)), float(os.environ.get("DFSHA_WAIT_MAX", 2)))

    def on_start(self):
        self.client = DFSClient(CONTROL, CA, workers=2)
        self.client.login(LOAD_USER, LOAD_PASSWORD)
        self.folder = f"/users/{LOAD_USER}/u_{uuid.uuid4().hex[:8]}"
        self.client.mkdir(self.folder)
        self.files: list[str] = []

    def on_stop(self):
        self.client.close()

    def medir(self, nombre: str, fn):
        """Ejecuta una operación del SDK y le reporta a Locust su tiempo y si falló."""
        start = time.perf_counter()
        error = None
        size = 0
        try:
            result = fn()
            size = len(result) if isinstance(result, (bytes, list)) else 0
        except Exception as exc:  # cualquier error cuenta como petición fallida
            error = exc
        events.request.fire(request_type="DFS", name=nombre, response_time=(time.perf_counter() - start) * 1000,
                            response_length=size, exception=error, context={})

    @task(2)
    def subir(self):
        size = random.choice(SIZES)
        path = f"{self.folder}/f_{uuid.uuid4().hex[:8]}.bin"
        data = os.urandom(size)
        self.medir(f"put {size // 1024} KB", lambda: self.client.put_bytes(data, path))
        self.files.append(path)

    @task(5)
    def descargar(self):
        if self.files:
            path = random.choice(self.files)
            self.medir("get", lambda: self.client.get_bytes(path))

    @task(3)
    def listar(self):
        self.medir("ls", lambda: self.client.ls(self.folder))
