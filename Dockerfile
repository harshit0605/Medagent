# Production image for the three Python services (orchestrator, gateway,
# scheduler). They share one codebase + one dependency set (the base
# `dependencies` in pyproject cover all three), so a single image runs any of
# them — pick the service at runtime via the SERVICE env var.
#
#   docker run -e SERVICE=orchestrator    -e PORT=8000 medagent
#   docker run -e SERVICE=whatsapp_gateway -e PORT=8000 medagent
#   docker run -e SERVICE=scheduler       -e PORT=8000 medagent
#
# In Coolify: build this Dockerfile once per app and set SERVICE (+ PORT) in
# each app's env. psycopg[binary] + cryptography + langgraph all ship wheels,
# so no apt build toolchain is needed on slim.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SERVICE=orchestrator \
    PORT=8000

WORKDIR /app

# Install deps first (cached unless packaging metadata changes). The package
# sources are needed because the hatchling build reads them.
COPY pyproject.toml README.md ./
COPY app ./app
COPY services ./services
COPY shared ./shared
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
RUN pip install -e .

EXPOSE 8000

# exec form via sh so $SERVICE / $PORT expand at runtime.
CMD ["sh", "-c", "exec uvicorn services.${SERVICE}.main:app --host 0.0.0.0 --port ${PORT}"]
