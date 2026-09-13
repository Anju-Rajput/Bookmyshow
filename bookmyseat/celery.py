"""
Celery app for BookMySeat.

Ticket PDF generation + email delivery run here, off the request/response
cycle, so a booking never has to wait on SMTP or PDF rendering to complete
(see payments/tasks.py).

To actually run tasks asynchronously you need:
1. A message broker (Redis is assumed below - `redis-server` running, or a
   managed Redis instance with its URL in the CELERY_BROKER_URL env var).
2. A worker process: `celery -A bookmyseat worker -l info` (run this
   alongside `manage.py runserver`, not instead of it).

Without a broker/worker, and with CELERY_TASK_ALWAYS_EAGER=True (the local
dev default when REDIS isn't configured - see settings.py), tasks instead
run synchronously inline. That's fine for development, but defeats the
"never block the booking process" requirement, so production MUST run a
real worker.
"""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bookmyseat.settings')

app = Celery('bookmyseat')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
