FROM python:3.12-slim

# 1. update → 2. install (w tym libpq-dev) → 3. clean
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
