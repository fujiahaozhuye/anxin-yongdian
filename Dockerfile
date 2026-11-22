# 安心用电 · 容器化部署（任意云服务器 / 阿里云 / 腾讯云 / 群晖均可）
# 构建： docker build -t anxin-yongdian .
# 运行： docker run -d -p 8000:8000 -e DEEPSEEK_API_KEY=sk-xxx anxin-yongdian
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# Pillow 仅用于把二维码导出为 PNG；其余全部为 Python 标准库
RUN pip install --no-cache-dir pillow==12.0.0

COPY . /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys;    \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["python", "app.py"]
