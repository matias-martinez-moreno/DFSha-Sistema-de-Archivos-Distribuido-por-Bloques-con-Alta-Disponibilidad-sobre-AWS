# Una sola imagen para ControlNode y DataNode; el comando decide cuál arranca:
#   docker run ... dfsha python -m dfsha.controlnode
#   docker run ... dfsha python -m dfsha.datanode
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

VOLUME ["/data"]
CMD ["python", "-m", "dfsha.controlnode"]
