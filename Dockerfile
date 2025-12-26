FROM python:3.11-slim

# Install system deps for TA-Lib
RUN apt-get update && \
    apt-get install -y build-essential libta-lib0 libta-lib-dev gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project
COPY . /app

# Create virtualenv and install
RUN python -m pip install --upgrade pip && \
    pip install -r requirements.txt

# Use .env for configuration
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
