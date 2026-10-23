FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el código fuente del scrapper y asegura la carpeta de scraps
COPY . .
RUN mkdir -p /app/scraps

# /app en PYTHONPATH (main.py importa s3_uploader desde su propio directorio src/)
ENV PYTHONPATH=/app

CMD ["python", "src/main.py"]