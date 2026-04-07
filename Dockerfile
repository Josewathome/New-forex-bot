FROM python:3.10-slim

WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        curl \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY src/requirements.txt .

RUN pip install --no-cache-dir --default-timeout=100 -r requirements.txt && \
    pip install --no-cache-dir --default-timeout=100 mt5linux==1.0.3

COPY src/ ./src/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]