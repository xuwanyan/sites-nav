FROM python:3.12.7-slim

WORKDIR /app

# 先装依赖，利用 Docker 层缓存（依赖变动少时重建快）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码与静态资源
COPY app.py ./
COPY static/ ./static/

# 数据目录（运行时由 docker-compose 的 volume 覆盖）
RUN mkdir -p /app/data

# 非 root 用户运行（降低容器被击破后的影响）
RUN addgroup --system app && adduser --system --ingroup app app && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
