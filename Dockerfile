FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATA_DIR=/data

WORKDIR /app

# 仅依赖标准库；拷贝应用、测试与核对脚本。
COPY app/ ./app/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
RUN chmod +x ./scripts/verify.sh && useradd -r -u 10001 appuser \
    && mkdir -p /data && chown appuser /data

USER appuser
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=3s --retries=10 \
  CMD python -c "import os,sys,urllib.request; p=os.environ.get('PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=2).status == 200 else 1)"

CMD ["python", "-m", "app.server"]
