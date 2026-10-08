"""Almacenamiento local de bloques en el disco del DataNode.

Cada bloque es un archivo <id>.blk cifrado. Se reparten en subcarpetas por los
dos primeros caracteres del id para no tener miles de archivos en una sola.
"""

import os
import re
from pathlib import Path

from dfsha.common.security import BlockCipher

BLOCK_ID = re.compile(r"^[0-9a-f]{32}$")


class BlockStore:
    def __init__(self, root: Path, cipher: BlockCipher):
        self.root = root / "blocks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.cipher = cipher

    def path_for(self, block_id: str) -> Path:
        # Validar el id evita que alguien pida rutas como ../../etc/passwd
        if not BLOCK_ID.match(block_id):
            raise ValueError("Id de bloque inválido")
        return self.root / block_id[:2] / f"{block_id}.blk"

    def put(self, block_id: str, data: bytes) -> None:
        path = self.path_for(block_id)
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(self.cipher.encrypt(data, block_id))
        os.replace(tmp, path)  # el bloque aparece completo o no aparece

    def get(self, block_id: str) -> bytes | None:
        path = self.path_for(block_id)
        if not path.exists():
            return None
        return self.cipher.decrypt(path.read_bytes(), block_id)

    def delete(self, block_id: str) -> None:
        path = self.path_for(block_id)
        path.unlink(missing_ok=True)

    def list_ids(self) -> list[str]:
        return [p.stem for p in self.root.glob("*/*.blk")]

    def used_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.glob("*/*.blk"))
