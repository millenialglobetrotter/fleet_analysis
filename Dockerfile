FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY requirements.deploy.txt ./requirements.deploy.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.deploy.txt

COPY fleet_analysis.py ./fleet_analysis.py
COPY config.example.json ./config.example.json

EXPOSE 8000

CMD ["python", "fleet_analysis.py"]