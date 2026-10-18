FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JANA_DATA_DIR=/app/data \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_CLIENT_TOOLBAR_MODE=minimal \
    STREAMLIT_THEME_PRIMARY_COLOR=#2F5BEA \
    STREAMLIT_THEME_BASE=light

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Справочники лежат внутри образа — в рантайме сеть не нужна
COPY data/seed ./data/seed
COPY data/demo ./data/demo
# учётные записи операторов: только соль и хеш пароля (scripts/add_user.py)
COPY data/users.json ./data/
COPY data/gazetteer.sqlit[e] data/core_gazetteer.jso[n] ./data/
# Ядро разбора (Участник 1): только рантайм, без tests/ и evaluate.py
COPY core/address_parser.py core/gazetteer.py ./core/
COPY app ./app
COPY ui ./ui

# Не root: код и справочники только для чтения, писать можно лишь в папку правок (том Docker)
RUN useradd --create-home --uid 10001 jana \
    && mkdir -p /app/data/corrections \
    && chown -R jana /app/data/corrections
USER jana

EXPOSE 8000 8501
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
