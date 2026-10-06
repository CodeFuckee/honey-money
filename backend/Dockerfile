FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
# pypi.org 官方 CDN 本网络实测 ~14KB/s 不可用，固定阿里源（Dockerfile.test 同）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY app/ app/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
