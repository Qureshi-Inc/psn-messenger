FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py psn_auth.py psn_messaging.py roast_bot.py portal.py psn_data.py mattermost.py ./
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/health')" || exit 1
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "3000"]
