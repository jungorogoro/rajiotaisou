FROM python:3.11-slim

WORKDIR /app

# システム依存ライブラリ
RUN apt-get update && apt-get install -y \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# app/requirements.txt をコピー
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# アプリ本体
COPY app /app/app

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "app.main"]
