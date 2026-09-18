# Revisar y actualizar el digest deliberadamente al actualizar la base.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS builder

WORKDIR /build
COPY pyproject.toml README.md requirements-production.txt ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install -c requirements-production.txt .

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

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
#
# --proxy-headers --forwarded-allow-ips='*': sin esto, request.client.host es
# la IP del proxy de Render en TODAS las peticiones, nunca la de quien
# pregunta -- confirmado, no habia ninguna bandera de esto antes. Rompia en
# silencio dos cosas: la atribucion de auditoria por IP para quien llega sin
# cookie (todo el mundo caia en el mismo balde) y volvia inutil cualquier
# limite por IP (ver api/rate_limit.py). Confiar en '*' aqui es correcto
# porque el contenedor no recibe trafico que no haya pasado primero por el
# borde de Render -- no es un servidor con IP publica propia.
# exec entrega las senales de apagado a Uvicorn para cerrar solicitudes y pools.
# En un Droplet, publicar solo en loopback y sobrescribir las cabeceras en
# el proxy de entrada (deploy/nginx-api.conf).
CMD ["sh", "-c", "exec uvicorn mira_api.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
