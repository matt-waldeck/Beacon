FROM python:3.12-slim
LABEL org.opencontainers.image.title="Beacon" org.opencontainers.image.version="0.3.3"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py index.html ./
ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
# One worker on purpose: the collector thread starts with the app and must run only once.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "app:app"]
