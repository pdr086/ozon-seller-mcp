FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OZON_CONFIG_FILE=/data/credentials.json

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ozon_seller ./ozon_seller
COPY skills ./skills
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["python", "-m", "ozon_seller"]
CMD ["serve", "--host", "0.0.0.0"]
