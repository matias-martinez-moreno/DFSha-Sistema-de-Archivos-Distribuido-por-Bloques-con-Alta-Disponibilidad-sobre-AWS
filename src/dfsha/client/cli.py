"""CLI de DFSha.

Se puede usar de dos formas:
    dfsha                      abre el shell interactivo (dfsha:/users/ana> ...)
    dfsha <comando> [args]     ejecuta un solo comando, útil para scripts

La configuración y la sesión se guardan en ~/.dfsha/session.json
(o en la carpeta que diga la variable DFSHA_HOME).
"""

import json
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path

from dfsha.client.sdk import DFSClient, DFSError
from dfsha.common import paths

HELP = """Comandos disponibles:
  config <url1,url2> [ca.crt]   configura los ControlNodes y el certificado de la CA
  login <usuario>               inicia sesión (pide la contraseña)
  logout | whoami
  ls [ruta]                     lista un directorio
  cd <ruta> | pwd               cambia o muestra el directorio actual
  mkdir <ruta> | rmdir <ruta>   crea o borra un directorio vacío
  rm <ruta>                     borra un archivo
  put <local> [remoto]          sube un archivo (lo parte en bloques)
  get <remoto> [local]          descarga un archivo (lo arma desde los bloques)
  cat <ruta> [inicio] [bytes]   lee un archivo o solo un rango de bytes
  write <ruta> <texto>          escribe un archivo nuevo con open/write/close
  info <ruta>                   muestra en qué DataNodes está cada bloque
  lock <ruta> <r|w> [segundos]  toma un lock | unlock <lock_id> lo suelta
  chmod <modo> <ruta>           cambia permisos, ej: chmod 750 docs
  chgrp <grupo> <ruta>          cambia el grupo
  useradd <usuario> <clave> [grupo1,grupo2]   (solo admin)
  nodes                         estado de ControlNodes y DataNodes
  help | exit"""


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


class Session:
    """Lo que el cliente recuerda entre comandos: ControlNodes, CA, token y directorio actual."""

    def __init__(self):
        home = Path(os.environ.get("DFSHA_HOME", Path.home() / ".dfsha"))
        self.file = home / "session.json"
        self.data = {"control_urls": [], "ca_file": None, "token": None, "user": None, "cwd": "/"}
        if self.file.exists():
            self.data.update(json.loads(self.file.read_text(encoding="utf-8")))
        # Las variables de entorno mandan sobre lo guardado
        if os.environ.get("DFSHA_CONTROL"):
            self.data["control_urls"] = os.environ["DFSHA_CONTROL"].split(",")
        if os.environ.get("DFSHA_CA"):
            self.data["ca_file"] = os.environ["DFSHA_CA"]
        self._client: DFSClient | None = None

    def save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    @property
    def client(self) -> DFSClient:
        if self._client is None:
            if not self.data["control_urls"]:
                raise DFSError("Primero configure los ControlNodes: config https://ip1:8000,https://ip2:8000 certs/ca.crt")
            self._client = DFSClient(self.data["control_urls"], self.data["ca_file"], self.data["token"])
        return self._client

    def path(self, p: str | None) -> str:
        return paths.resolve(self.data["cwd"], p or ".")


def need(args: list[str], n: int, usage: str) -> None:
    if len(args) < n:
        raise DFSError(f"Uso: {usage}")


def cmd_config(s: Session, args):
    need(args, 1, "config <url1,url2> [ca.crt]")
    s.data["control_urls"] = [u.strip().rstrip("/") for u in args[0].split(",") if u.strip()]
    if len(args) > 1:
        s.data["ca_file"] = str(Path(args[1]).resolve())
    s._client = None
    s.save()
    print(f"ControlNodes: {', '.join(s.data['control_urls'])}")
    print(f"CA: {s.data['ca_file'] or '(sin TLS)'}")


def cmd_login(s: Session, args):
    need(args, 1, "login <usuario>")
    import getpass
    password = args[1] if len(args) > 1 else getpass.getpass("Contraseña: ")
    s.client.login(args[0], password)
    me = s.client.me()
    s.data.update(token=s.client.token, user=me["username"], cwd=me["home"])
    s.save()
    print(f"Sesión iniciada como {me['username']} (grupos: {', '.join(me['groups'])}). Directorio: {me['home']}")


def cmd_logout(s: Session, args):
    s.data.update(token=None, user=None, cwd="/")
    s._client = None
    s.save()
    print("Sesión cerrada")


def cmd_whoami(s: Session, args):
    me = s.client.me()
    print(f"{me['username']}  grupos: {', '.join(me['groups'])}  admin: {'sí' if me['is_admin'] else 'no'}")


def cmd_ls(s: Session, args):
    entries = s.client.ls(s.path(args[0] if args else None))
    for e in entries:
        when = datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M")
        name = e["name"] + ("/" if e["kind"] == "dir" else "")
        print(f"{e['mode_str']}  {e['owner']:<10} {e['group']:<12} {human(e['size']):>9}  {when}  {name}")
    if not entries:
        print("(vacío)")


def cmd_cd(s: Session, args):
    target = s.path(args[0] if args else "/")
    info = s.client.stat(target)
    if info["kind"] != "dir":
        raise DFSError(f"{target} no es un directorio")
    s.data["cwd"] = target
    s.save()


def cmd_pwd(s: Session, args):
    print(s.data["cwd"])


def cmd_mkdir(s: Session, args):
    need(args, 1, "mkdir <ruta>")
    print(f"Creado {s.client.mkdir(s.path(args[0]))['path']}")


def cmd_rmdir(s: Session, args):
    need(args, 1, "rmdir <ruta>")
    print(f"Borrado {s.client.rmdir(s.path(args[0]))['path']}")


def cmd_rm(s: Session, args):
    need(args, 1, "rm <ruta>")
    print(f"Borrado {s.client.rm(s.path(args[0]))['path']}")


def cmd_put(s: Session, args):
    need(args, 1, "put <archivo_local> [ruta_remota]")
    local = args[0]
    if not os.path.isfile(local):
        raise DFSError(f"No existe el archivo local {local}")
    remote = s.path(args[1] if len(args) > 1 else os.path.basename(local))
    if s.client.exists(remote) and s.client.stat(remote)["kind"] == "dir":
        remote = paths.normalize(f"{remote}/{os.path.basename(local)}")
    size = os.path.getsize(local)
    print(f"Subiendo {local} ({human(size)}) a {remote} ...")
    r = s.client.put(local, remote)
    speed = r["size"] / max(r["seconds"], 0.001) / 1024 / 1024
    print(f"OK: {r['blocks']} bloques, {r['seconds']} s ({speed:.1f} MB/s)")


def cmd_get(s: Session, args):
    need(args, 1, "get <ruta_remota> [archivo_local]")
    remote = s.path(args[0])
    local = args[1] if len(args) > 1 else paths.name(remote)
    if os.path.isdir(local):
        local = os.path.join(local, paths.name(remote))
    print(f"Descargando {remote} a {local} ...")
    r = s.client.get(remote, local)
    speed = r["size"] / max(r["seconds"], 0.001) / 1024 / 1024
    print(f"OK: {human(r['size'])} en {r['blocks']} bloques, {r['seconds']} s ({speed:.1f} MB/s), checksums verificados")


def cmd_cat(s: Session, args):
    need(args, 1, "cat <ruta> [inicio] [bytes]")
    start = int(args[1]) if len(args) > 1 else 0
    count = int(args[2]) if len(args) > 2 else -1
    with s.client.open(s.path(args[0]), "r") as f:
        data = f.pread(start, count)
    sys.stdout.write(data.decode("utf-8", errors="replace"))
    if not data.endswith(b"\n"):
        sys.stdout.write("\n")


def cmd_write(s: Session, args):
    need(args, 2, "write <ruta> <texto>")
    path = s.path(args[0])
    with s.client.open(path, "w") as f:
        f.write(" ".join(args[1:]) + "\n")
    print(f"Escrito {path}")


def cmd_info(s: Session, args):
    need(args, 1, "info <ruta>")
    info = s.client.blocks(s.path(args[0]))
    print(f"{info['path']}  {human(info['size'])}  versión {info['version']}  "
          f"{len(info['blocks'])} bloques de hasta {human(info['block_size'])}")
    for b in info["blocks"]:
        print(f"  bloque {b['idx']:<4} {b['block_id'][:12]}  {human(b['size']):>9}  -> {', '.join(b['locations']) or 'SIN RÉPLICAS VIVAS'}")


def cmd_lock(s: Session, args):
    need(args, 2, "lock <ruta> <r|w> [segundos]")
    ttl = float(args[2]) if len(args) > 2 else 300
    lock_id = s.client.lock(s.path(args[0]), args[1], ttl)
    print(f"Lock {args[1]} tomado por {ttl:.0f} s. Id: {lock_id}")


def cmd_unlock(s: Session, args):
    need(args, 1, "unlock <lock_id>")
    s.client.unlock(args[0])
    print("Lock liberado")


def cmd_chmod(s: Session, args):
    need(args, 2, "chmod <modo_octal> <ruta>")
    r = s.client.chmod(s.path(args[1]), args[0])
    print(f"{r['path']} ahora tiene modo {oct(r['mode'])[2:]}")


def cmd_chgrp(s: Session, args):
    need(args, 2, "chgrp <grupo> <ruta>")
    r = s.client.chgrp(s.path(args[1]), args[0])
    print(f"{r['path']} ahora es del grupo {r['group']}")


def cmd_useradd(s: Session, args):
    need(args, 2, "useradd <usuario> <clave> [grupo1,grupo2]")
    groups = args[2].split(",") if len(args) > 2 else []
    r = s.client.useradd(args[0], args[1], groups)
    print(f"Usuario {r['username']} creado. Grupos: {', '.join(r['groups'])}. Home: {r['home']}")


def cmd_nodes(s: Session, args):
    info = s.client.cluster()
    print("ControlNodes:")
    for cn in info["controlnodes"]:
        print(f"  {cn.get('node_id', '?'):<14} {cn.get('role', '?'):<9} epoch {cn.get('epoch', '-')}  "
              f"operaciones en el log: {cn.get('last_seq', '-')}")
    print(f"DataNodes (factor de replicación {info['replication_factor']}):")
    for dn in info["datanodes"]:
        state = "UP" if dn["alive"] else "DOWN"
        print(f"  {dn['id']:<12} {state:<5} {dn['blocks']:>5} bloques  {human(dn['used_bytes']):>9}  "
              f"último heartbeat hace {dn['last_seen_seconds']} s  {dn['url']}")
    print(f"Archivos: {info['files']}  Bloques: {info['blocks']}  "
          f"Bloques con pocas réplicas: {info['under_replicated']}  Bloques perdidos: {info['lost_blocks']}  "
          f"Locks activos: {info['active_locks']}")


def cmd_help(s: Session, args):
    print(HELP)


COMMANDS = {
    "config": cmd_config, "login": cmd_login, "logout": cmd_logout, "whoami": cmd_whoami,
    "ls": cmd_ls, "cd": cmd_cd, "pwd": cmd_pwd, "mkdir": cmd_mkdir, "rmdir": cmd_rmdir, "rm": cmd_rm,
    "put": cmd_put, "get": cmd_get, "cat": cmd_cat, "write": cmd_write, "info": cmd_info,
    "lock": cmd_lock, "unlock": cmd_unlock, "chmod": cmd_chmod, "chgrp": cmd_chgrp,
    "useradd": cmd_useradd, "nodes": cmd_nodes, "help": cmd_help,
}


def run(s: Session, argv: list[str]) -> int:
    name, args = argv[0], argv[1:]
    fn = COMMANDS.get(name)
    if fn is None:
        print(f"Comando desconocido: {name}. Escriba help para ver los comandos.")
        return 1
    try:
        fn(s, args)
        return 0
    except DFSError as exc:
        code = f" ({exc.status})" if exc.status else ""
        print(f"Error{code}: {exc}")
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}")
    return 1


def shell(s: Session) -> None:
    print("DFSha shell. Escriba help para ver los comandos y exit para salir.")
    while True:
        try:
            line = input(f"dfsha:{s.data['cwd']}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("exit", "quit"):
            break
        try:
            argv = split_line(line)
        except ValueError as exc:
            print(f"Error: {exc}")
            continue
        run(s, argv)


def split_line(line: str) -> list[str]:
    # En Windows las rutas usan \ y el modo posix de shlex se las comería
    if os.name == "nt":
        return [part.strip('"').strip("'") for part in shlex.split(line, posix=False)]
    return shlex.split(line)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    session = Session()
    if len(sys.argv) > 1:
        sys.exit(run(session, sys.argv[1:]))
    shell(session)


if __name__ == "__main__":
    main()
