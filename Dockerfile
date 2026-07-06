FROM python:3.12-slim

ARG APP_ENV=production
ENV APP_ENV=${APP_ENV} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps: gcc for cryptography, curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

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
