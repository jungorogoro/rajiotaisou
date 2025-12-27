FROM python:3.11-slim

WORKDIR /app

# システム依存ライブラリ（Pillow などに必要なもの）
RUN apt-get update && apt-get install -y \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# ルートの requirements をコピー
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# アプリ本体
COPY app /app/app

ENV PYTHONUNBUFFERED=1

# Koyeb では Procfile でもよいが、ここでは main.py を直接起動
CMD ["python", "-m", "app.main"]
