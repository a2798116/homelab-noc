FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 交换机采集走 SSH：Comware 5.20 只认老 kex，用 3.5.1（4.x 起删掉了 group14-sha1）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        paramiko==3.5.1

COPY app /app

EXPOSE 8940

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8940/api/health',timeout=3).status==200 else 1)"

CMD ["python", "server.py"]
