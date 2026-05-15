FROM python:3.12-slim

WORKDIR /app

# Install system deps required by snowflake-connector-python's cryptography wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "server.py"]
