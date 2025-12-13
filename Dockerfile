# 1. Python公式イメージを使う
FROM python:3.11-slim

# 2. 作業ディレクトリ
WORKDIR /app

# 更新・日本語化
RUN apt-get update && apt-get -y install locales && apt-get -y upgrade && \
	localedef -f UTF-8 -i ja_JP ja_JP.UTF-8
ENV LANG ja_JP.UTF-8
ENV LANGUAGE ja_JP:ja
ENV LC_ALL ja_JP.UTF-8
ENV TZ Asia/Tokyo
ENV TERM xterm

# 3. 必要なファイルをコピー
COPY requirements.txt /app/
COPY . /app/

# 4. ライブラリのインストール
RUN pip install --no-cache-dir -r requirements.txt

# dataフォルダがないとエラーになるので作成
RUN mkdir -p /app/data

# ポート開放 (uvicornで指定したポート)
EXPOSE 8080

# 5. Botを起動（ファイル名はあなたのBotで合わせる）
CMD ["python", "app/main.py"]
# もし bot.py なら：
