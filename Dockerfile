FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    pandoc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

RUN mkdir -p pop-data/POP_Work/Data pop-data/POP_Work/Workdir

VOLUME ["/app/pop-data"]

EXPOSE 8032

CMD ["uvicorn", "pop_server:app", "--host", "0.0.0.0", "--port", "8032"]
