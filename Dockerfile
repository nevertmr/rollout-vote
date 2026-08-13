FROM python:3.12-slim

# 외부 의존성 0 (표준 라이브러리만 사용) — pip install 없음
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VOTE_PORT=8080 \
    VOTE_HOST=0.0.0.0 \
    VOTE_CLIPS=/clips \
    VOTE_POOL=/data/vote_pool.json \
    VOTE_DB=/data/vote.db

WORKDIR /app

# 앱 파일만 COPY — 영상(클립)은 이미지에 굽지 않고 bind mount 로 붙인다
COPY app.py vote_config.py vote_db.py vote_serving.py index.html /app/
COPY static /app/static

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3)\
.read().strip()==b'ok' else 1)"

CMD ["python3", "-u", "/app/app.py"]
