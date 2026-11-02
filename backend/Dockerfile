FROM python:3.12-slim

WORKDIR /srv

# git：定投回测日更的 Pages 快照自动发布（clone/push site-data）
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# pypi.org 官方 CDN 本网络实测 ~14KB/s 不可用，固定阿里源（Dockerfile.test 同）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
# vectorbt 单独装（元数据与 pandas 2.x 锁版冲突，运行时依赖上一行已显式给出）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ --no-deps vectorbt==1.1.1

COPY app/ app/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
