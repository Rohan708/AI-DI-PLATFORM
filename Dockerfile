# Lumida: the self-hosted image. Runs `lumida serve` (the nightly scheduler) by default.
#
#   docker build -t lumida:local .
#   docker build -t lumida:local --build-arg EXTRAS=mysql,oracle,snowflake .   (the default)
#
# SQL Server needs Microsoft's ODBC driver and its licence; it ships as a separate image
# variant later (see docs/setup/install.md).

ARG PYTHON_VERSION=3.12

# --- build the wheel ------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build==1.2.2
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /wheels

# --- runtime --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim
ARG EXTRAS="mysql,oracle,snowflake"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LUMIDA_LOG_FORMAT=json \
    LUMIDA_HEALTH_HOST=127.0.0.1 \
    LUMIDA_HEALTH_PORT=8080     LUMIDA_API_HOST=0.0.0.0     LUMIDA_API_PORT=8000

RUN useradd --create-home --uid 10001 lumida
COPY --from=build /wheels /wheels
RUN wheel="$(ls /wheels/*.whl)" \
    && if [ -n "$EXTRAS" ]; then target="${wheel}[${EXTRAS}]"; else target="$wheel"; fi \
    && pip install --no-cache-dir "$target" \
    && rm -rf /wheels

USER lumida
WORKDIR /home/lumida

# Healthy while the scheduler loop is alive (failing customer databases are alerted, not
# treated as the container being broken).
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

# The API (on only when LUMIDA_API_TOKEN is set). Publish it on the host's localhost, or
# behind your own reverse proxy with TLS; see docs/setup/install.md.
EXPOSE 8000

ENTRYPOINT ["lumida"]
CMD ["serve"]
