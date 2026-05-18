# Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY ./app /app
RUN -m pip install --no-cache-dir -r requirements.txt
EXPOSE 5000
CMD ["python", "main.py"]