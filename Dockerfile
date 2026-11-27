FROM python:3.12-slim
LABEL org.opencontainers.image.title="Beacon" org.opencontainers.image.version="1.0.0"
WORKDIR /app
# ping needs a raw-socket permission (set on the program itself, so Beacon can run without root); gosu drops root at start-up
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping libcap2-bin gosu && rm -rf /var/lib/apt/lists/* \
    && (setcap cap_net_raw+ep "$(readlink -f "$(command -v ping)")" || echo "Could not set the ping permission; ping will use unprivileged ICMP sockets instead") \
    && useradd --system --uid 10001 --shell /usr/sbin/nologin beacon
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Chart.js is bundled at build time so the dashboard works with no internet. The download must match this checksum (the same one the page checks if it ever has to load Chart.js from the CDN);
# if it can't be downloaded or doesn't match, nothing is bundled and the page falls back to the CDN, then to simple built-in charts.
ARG CHARTJS_VERSION=4.4.0
ARG CHARTJS_SRI=sha384-FcQlsUOd0TJjROrBxhJdUhXTUgNJQxTMcxZe6nHbaEfFL1zjQ+bq/uRoBQxb0KMo
RUN mkdir -p vendor && (python -c "import base64, hashlib, sys, urllib.request as u; d = u.urlopen('https://cdn.jsdelivr.net/npm/chart.js@${CHARTJS_VERSION}/dist/chart.umd.js', timeout=30).read(); h = 'sha384-' + base64.b64encode(hashlib.sha384(d).digest()).decode(); sys.exit('checksum mismatch: got ' + h) if h != '${CHARTJS_SRI}' else open('vendor/chart.umd.min.js', 'wb').write(d)" || echo "WARNING: Chart.js was not bundled (download failed or checksum mismatch); the page will use the CDN or the built-in charts")
COPY app.py index.html entrypoint.sh ./
# COPY keeps the permissions the files have on the host; make sure the unprivileged user can read everything, whatever they were
RUN chmod -R a+rX /app && chmod 755 /app/entrypoint.sh
ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
ENTRYPOINT ["/app/entrypoint.sh"]
# One worker on purpose: the collector thread starts with the app and must run only once.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "app:app"]
