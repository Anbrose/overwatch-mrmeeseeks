# 服务器端镜像。部署见 deploy/deploy.sh 和 README「部署到云服务器」。
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
# opencv-python（RapidOCR 依赖）需要的系统库
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ .
RUN useradd -r -u 10001 meeseeks && mkdir -p /app/data && chown meeseeks /app/data
USER meeseeks
CMD ["python", "bot.py"]
