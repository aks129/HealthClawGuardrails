FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    "python-telegram-bot>=20.0" \
    requests

COPY bot.py .

CMD ["python", "bot.py"]
