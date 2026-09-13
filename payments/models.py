from django.db import models
from django.contrib.auth.models import User

from movies.models import Theater, Seat


class Payment(models.Model):
    """One record per payment *attempt* for a set of held seats.

    A user can retry a failed/cancelled payment, which creates a new
    Payment row (linked via `retry_count`) rather than mutating the old
    one - so the full retry history is preserved for the booking history
    page and for support/audit purposes.
    """
    STATUS_CREATED = 'created'
    STATUS_SUCCESS = 'successful'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_REFUNDED = 'refunded'
    STATUS_CHOICES = [
        (STATUS_CREATED, 'Created'),
        (STATUS_SUCCESS, 'Successful'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_REFUNDED, 'Refunded'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payments')
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name='payments')
    seats = models.ManyToManyField(Seat, related_name='payments')

    amount = models.DecimalField(max_digits=10, decimal_places=2, help_text="Amount in INR")
    currency = models.CharField(max_length=8, default='INR')

    # Razorpay identifiers - razorpay_payment_id is the definitive transaction ID.
    razorpay_order_id = models.CharField(max_length=64, unique=True)
    razorpay_payment_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    razorpay_signature = models.CharField(max_length=256, blank=True, null=True)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_CREATED)
    failure_reason = models.CharField(max_length=255, blank=True)
    refunded_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Amount refunded, if this payment was later refunded."
    )
    retry_count = models.PositiveIntegerField(
        default=0, help_text="How many prior payment attempts preceded this one for the same seats."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Revenue/cancellation reports always filter by status + a
            # created_at date range together - one composite index serves
            # both instead of the DB needing two separate lookups.
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['theater', 'status']),
        ]

    def __str__(self):
        return f'{self.razorpay_order_id} ({self.status})'

    @property
    def seat_numbers(self):
        return ', '.join(self.seats.order_by('seat_number').values_list('seat_number', flat=True))

    @property
    def transaction_id(self):
        """The user-facing transaction ID: the actual payment id once
        captured, otherwise the order id as a placeholder."""
        return self.razorpay_payment_id or self.razorpay_order_id


class Ticket(models.Model):
    """The generated PDF ticket for a successful Payment (covering every
    seat/Booking under that payment). Created + emailed asynchronously by a
    Celery task (payments/tasks.py) so a booking's HTTP response never
    waits on PDF rendering or SMTP."""

    EMAIL_PENDING = 'pending'
    EMAIL_SENT = 'sent'
    EMAIL_FAILED = 'failed'
    EMAIL_STATUS_CHOICES = [
        (EMAIL_PENDING, 'Pending'),
        (EMAIL_SENT, 'Sent'),
        (EMAIL_FAILED, 'Failed (retries exhausted)'),
    ]

    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name='ticket')
    booking_reference = models.CharField(max_length=32, unique=True, db_index=True)
    pdf_file = models.FileField(upload_to='tickets/%Y/%m/')
    qr_verification_url = models.URLField(blank=True)

    email_status = models.CharField(max_length=10, choices=EMAIL_STATUS_CHOICES, default=EMAIL_PENDING)
    email_attempts = models.PositiveIntegerField(default=0)
    last_email_error = models.CharField(max_length=500, blank=True)

    generated_at = models.DateTimeField(auto_now_add=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-generated_at']

    def __str__(self):
        return f'Ticket {self.booking_reference} ({self.email_status})'
