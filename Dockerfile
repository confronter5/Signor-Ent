FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Use python directly instead of gunicorn so the Telegram Application 
# can properly start its event loop alongside Flask
CMD ["python", "bot.py"]
