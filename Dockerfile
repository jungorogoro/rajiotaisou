# 1. Python公式イメージを使う
FROM python:3.11-slim

# 2. 作業ディレクトリ
WORKDIR /app

# 3. 必要なファイルをコピー
COPY requirements.txt /app/
COPY . /app/

# 4. ライブラリのインストール
RUN pip install --no-cache-dir -r requirements.txt

# dataフォルダがないとエラーになるので作成
RUN mkdir -p /app/data

# 5. Botを起動（ファイル名はあなたのBotで合わせる）
CMD ["python", "main.py"]
# もし bot.py なら：
