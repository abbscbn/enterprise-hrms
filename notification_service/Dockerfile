FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY notification_service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd --uid 10001 --create-home appuser && mkdir -p /var/lib/hrms-events && chown appuser /var/lib/hrms-events
COPY notification_service /app/notification_service
COPY backend/app/events /app/backend/app/events
USER appuser
EXPOSE 5000
CMD ["python", "-m", "notification_service.app"]
