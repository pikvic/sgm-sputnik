FROM node:22-alpine AS maplibre
WORKDIR /vendor
RUN npm pack maplibre-gl@5.10.0 --silent && tar -xzf maplibre-gl-5.10.0.tgz
RUN mkdir /markdown && cd /markdown && npm pack markdown-it@15.0.3 --silent && tar -xzf markdown-it-15.0.3.tgz

FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py catalog.py settings.py storage.py integrations.py assistant.py assistant_routes.py serve.py research_context.py gazetteer.py knowledge.json geological-objects.json ./
COPY static ./static
COPY --from=maplibre /vendor/package/dist/maplibre-gl.js /vendor/package/dist/maplibre-gl.css ./static/vendor/
COPY --from=maplibre /vendor/package/LICENSE.txt ./static/vendor/LICENSE.txt
COPY --from=maplibre /markdown/package/dist/browser/markdown-it.umd.min.js ./static/vendor/markdown-it.min.js
COPY --from=maplibre /markdown/package/LICENSE ./static/vendor/markdown-it-LICENSE.txt
USER 65532:65532
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"]
CMD ["python", "serve.py"]
