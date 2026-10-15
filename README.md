# DFSha

Sistema de archivos distribuido por bloques con alta disponibilidad, desplegado en AWS.
Proyecto 1 de Sistemas Distribuidos, EAFIT 2026-2. Opción 1: cliente/servidor. Matías Martínez Moreno.

Cada archivo se parte en bloques de 4 MB y cada bloque se guarda en 3 DataNodes distintos. Dos ControlNodes, un líder y un seguidor, guardan los metadatos. Todo va por REST sobre HTTPS, los bloques se guardan cifrados y hay permisos por usuario y grupo.

El informe está en [docs/informe.pdf](docs/informe.pdf).
Link video: https://youtu.be/EXfGDpnk5QI


## Estructura

```text
src/dfsha/client/        CLI y SDK
src/dfsha/controlnode/   metadatos, líder y seguidor, locks
src/dfsha/datanode/      almacenamiento de bloques
src/dfsha/common/        configuración, cifrado y TLS
tests/                   pruebas automáticas (pytest)
scripts/                 certificados, clúster local, archivos de prueba y benchmark
load/                    prueba de carga (Locust)
infra/terraform/         despliegue en AWS
docker-compose.yml       clúster local con 2 ControlNodes y 4 DataNodes
```

## Instalación

Requiere Python 3.11 o superior, Docker y Terraform (solo para AWS).

```bash
python -m venv .venv
.venv\Scripts\activate            # en Linux: source .venv/bin/activate
pip install -e ".[dev]"
python scripts/generar_seguridad.py
```

El último comando crea `certs/` y `.env` con los secretos y la clave del admin. No se suben al repositorio.

## Correrlo local

```bash
docker compose up -d --build
dfsha config https://localhost:8001,https://localhost:8002 certs/ca.crt
dfsha login admin                 # la clave está en .env
dfsha useradd ana clave123 equipo
dfsha login ana
dfsha                             # abre el shell
```

## Desplegarlo en AWS

1. Copiar las credenciales del Learner Lab ("AWS Details") en `~/.aws/credentials`.
2. El repositorio debe ser público, porque cada EC2 lo clona al arrancar.
3. Crear la infraestructura:

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # poner la clave del admin
terraform init
terraform apply
```

4. Esperar de 3 a 5 minutos y correr el comando que imprime `terraform output comando_config`.

Para escalar se cambia `datanode_count` y se aplica de nuevo. Para borrar todo, `terraform destroy`.

## Uso

```text
dfsha:/users/ana> put pruebas/libro.pdf
dfsha:/users/ana> info libro.pdf
dfsha:/users/ana> get libro.pdf descargas/
dfsha:/users/ana> cat libro.pdf 0 100
dfsha:/users/ana> lock libro.pdf w 60
dfsha:/users/ana> nodes
```

`help` muestra todos los comandos. Desde Python:

```python
from dfsha.client import DFSClient

c = DFSClient(["https://IP_CN1:8000", "https://IP_CN2:8000"], ca_file="certs/ca.crt")
c.login("ana", "clave123")
c.put("foto.jpg", "/users/ana/foto.jpg")
c.get("/users/ana/foto.jpg", "copia.jpg")
```

## Pruebas

```bash
pytest -v                                    # levanta un clúster local y corre las 33 pruebas
locust -f load/locustfile.py                 # carga, en http://localhost:8089
python scripts/benchmark.py --clave XXXX     # tiempos de subida y bajada
```

Los resultados están en la sección 13 del informe.
