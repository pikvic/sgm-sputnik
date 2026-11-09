FROM node:22-alpine AS maplibre
WORKDIR /vendor
RUN npm pack maplibre-gl@5.10.0 --silent && tar -xzf maplibre-gl-5.10.0.tgz

FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir gunicorn==25.1.0
COPY app.py catalog.py ./
COPY static ./static
COPY --from=maplibre /vendor/package/dist/maplibre-gl.js /vendor/package/dist/maplibre-gl.css ./static/vendor/
COPY --from=maplibre /vendor/package/LICENSE.txt ./static/vendor/LICENSE.txt
USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"]
CMD ["gunicorn", "--no-control-socket", "--bind=0.0.0.0:8080", "--worker-class=gthread", "--workers=2", "--threads=4", "--timeout=60", "app:application"]
