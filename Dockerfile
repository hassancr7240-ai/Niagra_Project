FROM python:3.12-slim

ARG APP_ENV=production
ENV APP_ENV=${APP_ENV} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Docling stores ML models here — baked into the image so first PDF upload is instant
    DOCLING_ARTIFACTS_PATH=/opt/docling-models \
    HF_HOME=/opt/docling-models/hf

WORKDIR /app

# System deps: gcc for cryptography, curl for healthcheck,
# Microsoft ODBC Driver 18 for Azure SQL Server connectivity
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    curl \
    gnupg2 \
    apt-transport-https \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Pre-download IBM Docling ML models into the image (avoids slow first-run download)
# Models are stored in DOCLING_ARTIFACTS_PATH (/opt/docling-models) — baked into layer.
COPY scripts/download_docling_models.py /tmp/download_docling_models.py
RUN python /tmp/download_docling_models.py

# Copy application source (NOT data/ — DB starts fresh in production)
COPY app/      ./app/
COPY frontend/ ./frontend/
COPY data/pm_library.json ./data/pm_library.json

# On Azure App Service Linux, /home is the ONLY persistent directory.
# We symlink our data + output dirs there so files survive container restarts.
RUN mkdir -p /home/data /home/output/pm-docs /home/uploads \
    && ln -sf /home/data    /app/data \
    && ln -sf /home/output  /app/output \
    && ln -sf /home/uploads /app/uploads

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# 2 workers = handles concurrent uploads + chat requests comfortably
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
