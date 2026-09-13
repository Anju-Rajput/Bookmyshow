import logging
import uuid

from celery import shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import EmailMessage
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)


def _generate_booking_reference():
    return f'BMS-{uuid.uuid4().hex[:10].upper()}'


def _absolute_verify_url(booking_reference):
    path = reverse('verify_ticket', args=[booking_reference])
    return f'{settings.SITE_BASE_URL.rstrip("/")}{path}'


@shared_task(bind=True)
def generate_ticket(self, payment_id):
    """Step 1: build the PDF and save the Ticket row. Kept separate from
    email sending so that a PDF (and the ability to download it) exists
    even if every email attempt later fails - the two concerns have very
    different failure modes and shouldn't be coupled."""
    from .models import Payment, Ticket
    from .ticket_pdf import build_ticket_pdf

    try:
        payment = Payment.objects.select_related('theater__movie', 'user').get(id=payment_id)
    except Payment.DoesNotExist:
        logger.error('generate_ticket: Payment %s not found', payment_id)
        return None

    if payment.status != Payment.STATUS_SUCCESS:
        logger.warning('generate_ticket: Payment %s is not successful (status=%s), skipping', payment_id, payment.status)
        return None

    # Idempotent: if a task retry or a duplicate webhook+callback race
    # already created the ticket, don't regenerate/re-save it - just reuse it.
    existing = Ticket.objects.filter(payment=payment).first()
    if existing:
        return existing.id

    booking_reference = _generate_booking_reference()
    verify_url = _absolute_verify_url(booking_reference)
    pdf_bytes = build_ticket_pdf(payment, booking_reference, verify_url)

    ticket = Ticket(
        payment=payment, booking_reference=booking_reference, qr_verification_url=verify_url,
    )
    ticket.pdf_file.save(f'{booking_reference}.pdf', ContentFile(pdf_bytes), save=False)
    ticket.save()

    send_ticket_email.delay(ticket.id)
    return ticket.id


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,        # 30s, 60s, 120s, 240s, 480s ...
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def send_ticket_email(self, ticket_id):
    """Step 2: email the already-generated PDF. Automatically retried by
    Celery (exponential backoff) on ANY exception - SMTP timeouts,
    connection refused, transient provider errors, etc. After the final
    retry is exhausted, the ticket is marked 'failed' but stays fully
    downloadable from the user's booking history regardless."""
    from .models import Ticket

    try:
        ticket = Ticket.objects.select_related('payment__user', 'payment__theater__movie').get(id=ticket_id)
    except Ticket.DoesNotExist:
        logger.error('send_ticket_email: Ticket %s not found', ticket_id)
        return

    ticket.email_attempts += 1
    ticket.save(update_fields=['email_attempts'])

    payment = ticket.payment
    user = payment.user
    movie = payment.theater.movie

    try:
        email = EmailMessage(
            subject=f'Your BookMySeat ticket for {movie.name}',
            body=(
                f'Hi {user.username},\n\n'
                f'Your booking for "{movie.name}" is confirmed.\n'
                f'Booking ID: {ticket.booking_reference}\n'
                f'Theater: {payment.theater.name}\n'
                f'Showtime: {payment.theater.time.strftime("%d %b %Y, %I:%M %p")}\n'
                f'Seats: {payment.seat_numbers}\n\n'
                f'Your e-ticket is attached as a PDF - you can also download it '
                f'anytime from your booking history.\n\n'
                f'Enjoy the movie!'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email] if user.email else [],
        )
        if not user.email:
            raise ValueError(f'User {user.username} has no email address on file.')

        ticket.pdf_file.open('rb')
        email.attach(f'{ticket.booking_reference}.pdf', ticket.pdf_file.read(), 'application/pdf')
        ticket.pdf_file.close()

        email.send(fail_silently=False)
    except Exception as exc:
        ticket.email_status = Ticket.EMAIL_FAILED if self.request.retries >= self.max_retries else Ticket.EMAIL_PENDING
        ticket.last_email_error = str(exc)[:500]
        ticket.save(update_fields=['email_status', 'last_email_error'])
        logger.warning(
            'send_ticket_email: attempt %s failed for ticket %s: %s',
            self.request.retries + 1, ticket_id, exc,
        )
        raise  # lets Celery's autoretry_for handle the backoff/retry

    ticket.email_status = Ticket.EMAIL_SENT
    ticket.emailed_at = timezone.now()
    ticket.last_email_error = ''
    ticket.save(update_fields=['email_status', 'emailed_at', 'last_email_error'])
