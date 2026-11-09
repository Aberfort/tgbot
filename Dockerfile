FROM python:3.12-slim

# Встановлюємо змінні оточення для Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Kyiv

WORKDIR /app

# Системні залежності для компіляції cryptg (прискорює роботу Telethon з медіа)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Спочатку копіюємо requirements.txt для кешування шарів Docker
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копіюємо код проєкту та промпти
COPY app/ ./app/
COPY prompts/ ./prompts/
COPY main.py .

# Створюємо директорії для томів (сесії, база даних, логи)
RUN mkdir -p /app/sessions /app/data/tmp_media /app/logs

CMD ["python", "main.py"]
