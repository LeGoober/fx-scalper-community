FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /app/data

EXPOSE 5001

CMD ["python", "-m", "app.server"]
