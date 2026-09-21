FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ytube.py .
# Koyeb injects $PORT; default 8099 for local runs. "lan" binds all interfaces.
CMD ["sh", "-c", "python3 ytube.py ${PORT:-8099} lan"]
