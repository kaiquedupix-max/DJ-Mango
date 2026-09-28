FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DENO_INSTALL=/usr/local

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 ca-certificates curl unzip \
    && curl -fsSL https://deno.land/install.sh | sh \
    && deno --version \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
