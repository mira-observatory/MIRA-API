FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

# Un solo worker a proposito: el trabajo es I/O-bound y asi el pool de conexiones y
# el cache en memoria no se fragmentan. Se escala con replicas, no con workers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

RUN useradd --create-home --uid 10001 mira
COPY --from=builder /install /usr/local

USER mira
WORKDIR /app
COPY scripts ./scripts
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz').status==200 else 1)"

# Forma shell (sh -c) a proposito: la forma exec no expande variables, y Render
# inyecta PORT y falla el despliegue con "no open ports detected" si el servicio
# no escucha justo ahi. El 8080 queda como valor por defecto para `docker run`
# a secas y para el EXPOSE de arriba.
CMD ["sh", "-c", "uvicorn mira_api.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
