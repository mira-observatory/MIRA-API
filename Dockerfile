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
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"

CMD ["uvicorn", "mira_api.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
