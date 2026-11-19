FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY graph.py backlog.json ./
RUN useradd -u 10001 -r -s /usr/sbin/nologin miner && mkdir -p /data && chown miner /data
USER 10001
ENTRYPOINT ["python", "graph.py"]
