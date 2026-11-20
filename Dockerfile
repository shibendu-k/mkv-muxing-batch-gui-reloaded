FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DATA_ROOT=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      gnupg \
      python3 \
      mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

# Prefer the official MKVToolNix repository for current upstream builds.
# The Ubuntu package above remains an available fallback when DNS or network
# access is temporarily unavailable.
RUN install -d -m 0755 /usr/share/keyrings; \
    if curl -fsSL https://mkvtoolnix.download/gpg-pub-moritzbunkus.txt \
       | gpg --dearmor -o /usr/share/keyrings/gpg-pub-moritzbunkus.gpg; then \
      echo "deb [signed-by=/usr/share/keyrings/gpg-pub-moritzbunkus.gpg arch=amd64] https://mkvtoolnix.download/ubuntu/noble/ ./ " \
        > /etc/apt/sources.list.d/mkvtoolnix.list; \
      apt-get update \
        && apt-get install -y --no-install-recommends --allow-downgrades mkvtoolnix \
        || true; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY web/ /app/web/
RUN mkdir -p /data && chmod 0775 /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=3)"

CMD ["python3", "/app/web/server.py"]
