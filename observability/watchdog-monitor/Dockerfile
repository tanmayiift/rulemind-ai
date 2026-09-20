FROM python:3.11-slim
WORKDIR /app
COPY monitor.py .
RUN adduser --system --uid 10003 watchdog
USER watchdog
CMD ["python3", "-u", "monitor.py"]
