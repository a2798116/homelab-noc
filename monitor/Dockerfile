FROM python:3.12-alpine
LABEL org.opencontainers.image.title="homelab-monitor" \
      org.opencontainers.image.description="家庭设备状态看板（温度/负载/磁盘）"

WORKDIR /app
COPY app/ /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8930 \
    DB_PATH=/data/monitor.db

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8930

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8930/api/health',timeout=4).status==200 else 1)"

CMD ["python", "/app/server.py"]
