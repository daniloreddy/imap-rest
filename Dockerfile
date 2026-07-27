FROM python:3.12-slim

RUN useradd --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser
ENV HOME=/home/appuser
WORKDIR /app

COPY requirements.txt ./
# git required by pip to fetch the redberry-webkit dependency (git+https:// pin in requirements.txt)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y git \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY app/ ./app/
COPY static/ ./static/
COPY scripts/ ./scripts/

RUN mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
