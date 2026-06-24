FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Manila

WORKDIR /app

# Install pip-tools for editable install
RUN pip install --upgrade pip

# Copy dependency manifest first for layer caching
COPY pyproject.toml ./
RUN pip install -e .

# Install Playwright browser (chromium only — saves space)
RUN playwright install chromium && playwright install-deps chromium

# Copy app code
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/

# Non-root user — reuse the pwuser that ships with the Playwright base image

RUN mkdir -p /app/ig_sessions && chown -R pwuser:pwuser /app
USER pwuser

# Default: run API. Workers are launched via docker-compose with explicit commands.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
