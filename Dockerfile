FROM python:3.14-slim

# Instalar dependencias del sistema necesarias para compilar paquetes si es necesario (ej. PyNaCl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar e instalar las dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código del bot, configuración y plan de estudios
COPY bot.py .
COPY config.json .
COPY planes_estudio.json .

# Comando para ejecutar el bot
CMD ["python", "bot.py"]
