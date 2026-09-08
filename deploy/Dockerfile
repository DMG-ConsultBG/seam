# Seam needs Flask, waitress and nothing else: no database server, no compiled
# extension, no build step. That is why this image is small and why it starts
# in a second.
FROM python:3.12-slim

# Chromium is what turns a document into a real PDF. Without it the download
# falls back to the browser's print dialogue - everything still works, the file
# just is not handed over ready to attach. Drop these two lines for a much
# smaller image if that trade is fine.
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The database, the uploads and the keys live on a mounted volume, not in the
# image: a container filesystem is thrown away on every deploy, and putting
# SQLite there means losing everything at the first restart.
ENV SEAM_DB=/data/seam.db \
    SEAM_HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# The platform's proxy terminates TLS and is the only thing that can reach this
# port, so it may be believed about whether the outside connection was
# encrypted. Without this the session cookie is not marked Secure.
#
# SEAM_PROXY_ONLY states the part the process cannot check for itself: that the
# port is not open to anyone else. It is true inside a container and it is what
# makes trusting any caller safe here.
ENV SEAM_TRUSTED_PROXY=* \
    SEAM_PROXY_ONLY=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

CMD ["python", "app.py"]
