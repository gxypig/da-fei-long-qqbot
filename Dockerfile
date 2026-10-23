FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 ASTRBOT_DISABLE_METRICS=1 TZ=Asia/Shanghai
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git libgomp1 ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN cp -a AstrBot/data/plugins /app/bundled_plugins
CMD ["python", "container_entry.py"]
