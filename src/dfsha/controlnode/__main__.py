"""Arranque del ControlNode: python -m dfsha.controlnode"""

import logging

import uvicorn

from dfsha.common import tls
from dfsha.controlnode.main import Settings, create_app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, log_level="warning",
                **tls.uvicorn_tls_kwargs(settings.cert_file, settings.key_file))
