# 服务器端镜像。部署见 deploy/deploy.sh 和 README「部署到云服务器」。
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ .
RUN useradd -r -u 10001 meeseeks
USER meeseeks
CMD ["python", "bot.py"]
