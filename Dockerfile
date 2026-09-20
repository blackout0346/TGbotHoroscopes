# Используем легковесный образ Python
FROM python:3.12-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем список зависимостей и устанавливаем их
COPY reg.txt .
RUN pip install --no-cache-dir -r reg.txt

# Копируем все остальные файлы проекта
COPY . .

# Команда для старта бота
CMD ["python", "main.py"]