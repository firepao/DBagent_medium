FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && addgroup --system agent \
    && adduser --system --ingroup agent --home /app agent

COPY --chown=agent:agent app ./app
COPY --chown=agent:agent config ./config
COPY --chown=agent:agent web ./web
RUN mkdir -p /app/runtime && chown agent:agent /app/runtime

USER agent
EXPOSE 8030
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8030", "--proxy-headers", "--forwarded-allow-ips=*"]
