# Одноразовый контейнер для сборки справочника из OSM (нужен интернет).
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
WORKDIR /work
COPY requirements-build.txt .
RUN pip install -r requirements-build.txt
COPY scripts ./scripts
# нормализация ключей (app.text.norm) нужна при ужатии зданий
COPY app/__init__.py app/text.py ./app/
CMD ["python", "scripts/build_gazetteer.py"]
