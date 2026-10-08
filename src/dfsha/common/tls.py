"""Configuración TLS para los clientes HTTP (cliente DFSha y llamadas entre nodos)."""

import ssl


def client_ssl_context(ca_file: str | None):
    """Devuelve el contexto TLS que valida los certificados contra nuestra CA.

    No se revisa el nombre del host porque en AWS las IP públicas cambian cada
    vez que se crean las máquinas. Sí se exige que el certificado esté firmado
    por la CA del proyecto, así que un servidor sin ese certificado es rechazado.
    """
    if not ca_file:
        return True
    context = ssl.create_default_context(cafile=ca_file)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def uvicorn_tls_kwargs(cert_file: str | None, key_file: str | None) -> dict:
    if cert_file and key_file:
        return {"ssl_certfile": cert_file, "ssl_keyfile": key_file}
    return {}
