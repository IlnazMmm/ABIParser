# Используем официальный образ Python 3.12 slim
FROM python:3.12-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем requirements (если нужен отдельный файл)
# COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir docker eth_abi

# Копируем весь проект в контейнер
COPY . .

# По умолчанию запускаем Python интерактивно
CMD ["python3"]