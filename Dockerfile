FROM python:3.12-slim

WORKDIR /app/backend

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/strategies ./strategies
RUN mkdir -p /app/backend/data

ENV COMMUNITY_HOST=0.0.0.0 \
    COMMUNITY_PORT=5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5001/api/health')" || exit 1

EXPOSE 5001

CMD ["python", "-m", "app"]
