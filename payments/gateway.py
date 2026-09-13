import razorpay
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def get_client():
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise ImproperlyConfigured(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. "
            "Get test-mode keys from the Razorpay dashboard and set them as "
            "environment variables before accepting payments."
        )
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
