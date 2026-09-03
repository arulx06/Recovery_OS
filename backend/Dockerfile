# RecoveryOS backend - same image for API and worker (different command)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for psycopg2, scikit-learn
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The artifact is intentionally not committed or copied from the host. Build it
# from pinned code/dependencies, then fail the image build if it cannot load.
RUN python -m app.ml.train && python -c "from app.ml.scorer import get_model_info; assert get_model_info().get('available') is True"

# Non-root for safety
RUN useradd -m app && chown -R app:app /app
USER app

EXPOSE 8000

# API: alembic upgrade head runs as one-time release step (see docker-compose.prod.yml)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
