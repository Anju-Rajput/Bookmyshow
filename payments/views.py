import json
from decimal import Decimal

import razorpay
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from movies.models import Theater, Seat
from movies.services import finalize_seat_bookings
from .models import Payment
from .gateway import get_client
from .tasks import generate_ticket


@login_required(login_url='/login/')
def create_payment_order(request, theater_id):
    """Starts (or resumes) a payment for whatever seats this user currently
    has on hold for this theater. Creates a Razorpay Order server-side and
    renders the Checkout page - no card/UPI details ever touch our server."""
    theater = get_object_or_404(Theater, id=theater_id)
    theater.release_expired_reservations()

    held_seats = Seat.objects.filter(theater=theater, reserved_by=request.user, is_booked=False)
    if not held_seats.exists():
        messages.info(request, "Your seat hold expired. Please select seats again.")
        return redirect('book_seats', theater_id=theater.id)

    held_seat_ids = set(held_seats.values_list('id', flat=True))
    amount_rupees = (Decimal(settings.SEAT_PRICE_RUPEES) * held_seats.count()).quantize(Decimal('0.01'))

    # Reuse a still-open order for the exact same seats (e.g. the user just
    # refreshed the page) instead of creating a fresh Razorpay order every time.
    previous = Payment.objects.filter(user=request.user, theater=theater).order_by('-created_at').first()
    reusable = (
        previous and previous.status == Payment.STATUS_CREATED
        and set(previous.seats.values_list('id', flat=True)) == held_seat_ids
    )

    if reusable:
        payment = previous
    else:
        try:
            client = get_client()
            order = client.order.create({
                'amount': int(amount_rupees * 100),  # paise
                'currency': 'INR',
                'payment_capture': 1,
            })
        except ImproperlyConfigured as exc:
            messages.error(request, str(exc))
            return redirect('confirm_booking', theater_id=theater.id)
        except razorpay.errors.BadRequestError as exc:
            messages.error(request, f"Could not start payment: {exc}")
            return redirect('confirm_booking', theater_id=theater.id)

        payment = Payment.objects.create(
            user=request.user, theater=theater, amount=amount_rupees,
            razorpay_order_id=order['id'],
            retry_count=(previous.retry_count + 1) if previous else 0,
        )
        payment.seats.set(held_seats)

    return render(request, 'payments/checkout.html', {
        'theater': theater,
        'payment': payment,
        'seats': held_seats,
        'amount_paise': int(payment.amount * 100),
        'razorpay_key_id': settings.RAZORPAY_KEY_ID,
    })


def _finalize_successful_payment(payment, razorpay_payment_id, razorpay_signature):
    """Idempotent: safe to call more than once for the same payment (e.g.
    both the browser's success callback and the Razorpay webhook firing for
    the same event)."""
    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(id=payment.id)
        if locked.status == Payment.STATUS_SUCCESS:
            return {'ok': True, 'already_processed': True, 'booked': [], 'unavailable': []}

        booked, unavailable = finalize_seat_bookings(locked.user, locked.theater, payment=locked)

        locked.razorpay_payment_id = razorpay_payment_id
        locked.razorpay_signature = razorpay_signature
        if booked:
            locked.status = Payment.STATUS_SUCCESS
            if unavailable:
                locked.failure_reason = (
                    f'Payment captured but these seats became unavailable: '
                    f'{", ".join(unavailable)} - please contact support for a refund on those.'
                )
        else:
            locked.status = Payment.STATUS_FAILED
            locked.failure_reason = 'Seat hold expired before payment could be confirmed.'
        locked.save()

        if locked.status == Payment.STATUS_SUCCESS:
            # Deferred until the transaction actually commits, so the Celery
            # worker (a separate process) is guaranteed to see this Payment
            # and its Bookings when it picks the task up. .delay() itself
            # just enqueues a message and returns immediately - the booking
            # request below does NOT wait for PDF generation or email.
            transaction.on_commit(lambda: generate_ticket.delay(locked.id))

    return {'ok': locked.status == Payment.STATUS_SUCCESS, 'booked': booked, 'unavailable': unavailable}


@login_required(login_url='/login/')
@require_POST
def verify_payment(request):
    """Called by the browser's Razorpay Checkout success handler. Verifies
    the HMAC signature server-side before ever trusting the payment - the
    browser cannot forge a valid signature without the secret key."""
    payment_id = request.POST.get('payment_db_id')
    razorpay_order_id = request.POST.get('razorpay_order_id')
    razorpay_payment_id = request.POST.get('razorpay_payment_id')
    razorpay_signature = request.POST.get('razorpay_signature')

    payment = get_object_or_404(
        Payment, id=payment_id, user=request.user, razorpay_order_id=razorpay_order_id
    )

    try:
        client = get_client()
        client.utility.verify_payment_signature({
            'razorpay_order_id': razorpay_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature,
        })
    except razorpay.errors.SignatureVerificationError:
        return JsonResponse({'ok': False, 'error': 'Payment signature verification failed.'}, status=400)
    except ImproperlyConfigured as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)

    result = _finalize_successful_payment(payment, razorpay_payment_id, razorpay_signature)
    return JsonResponse(result)


@login_required(login_url='/login/')
@require_POST
def mark_payment_failed(request, payment_id):
    """Called by the browser when the user cancels/closes the Razorpay
    checkout modal, or its payment.failed handler fires. Immediately frees
    the seat hold instead of waiting for the 2-minute TTL to lapse."""
    payment = get_object_or_404(Payment, id=payment_id, user=request.user)
    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(id=payment.id)
        if locked.status == Payment.STATUS_CREATED:
            locked.status = Payment.STATUS_CANCELLED
            locked.failure_reason = request.POST.get('reason', 'Cancelled by user')[:255]
            locked.save(update_fields=['status', 'failure_reason', 'updated_at'])
            Seat.objects.filter(
                theater=locked.theater, reserved_by=locked.user, is_booked=False
            ).update(reserved_by=None, reserved_at=None)
    return JsonResponse({'ok': True})


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """Server-to-server confirmation from Razorpay - the authoritative
    source of truth, independent of whether the customer's browser stayed
    on the page. Verified via HMAC signature using the webhook secret
    configured in the Razorpay dashboard (Settings -> Webhooks)."""
    signature = request.headers.get('X-Razorpay-Signature', '')
    body = request.body

    if not settings.RAZORPAY_WEBHOOK_SECRET:
        return HttpResponseBadRequest('Webhook secret not configured')

    try:
        client = get_client()
        client.utility.verify_webhook_signature(
            body.decode('utf-8'), signature, settings.RAZORPAY_WEBHOOK_SECRET
        )
    except razorpay.errors.SignatureVerificationError:
        return HttpResponseBadRequest('Invalid signature')
    except ImproperlyConfigured:
        return HttpResponseBadRequest('Payment gateway not configured')

    try:
        event = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest('Invalid payload')

    event_type = event.get('event')
    payload = event.get('payload', {}).get('payment', {}).get('entity', {})
    order_id = payload.get('order_id')
    razorpay_payment_id = payload.get('id')

    try:
        payment = Payment.objects.get(razorpay_order_id=order_id)
    except Payment.DoesNotExist:
        # Unknown order - acknowledge anyway so Razorpay doesn't keep retrying.
        return HttpResponse(status=200)

    if event_type == 'payment.captured':
        _finalize_successful_payment(payment, razorpay_payment_id, signature)
    elif event_type == 'payment.failed':
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(id=payment.id)
            if locked.status == Payment.STATUS_CREATED:
                locked.status = Payment.STATUS_FAILED
                locked.razorpay_payment_id = razorpay_payment_id
                locked.failure_reason = (payload.get('error_description') or 'Payment failed')[:255]
                locked.save()
                Seat.objects.filter(
                    theater=locked.theater, reserved_by=locked.user, is_booked=False
                ).update(reserved_by=None, reserved_at=None)

    return HttpResponse(status=200)


@login_required(login_url='/login/')
def download_ticket(request, payment_id):
    """Lets a user download the PDF ticket for any of their own past
    bookings from their booking history, independent of whether the email
    was ever successfully delivered."""
    payment = get_object_or_404(Payment, id=payment_id, user=request.user)
    ticket = getattr(payment, 'ticket', None)
    if ticket is None:
        messages.info(request, "Your ticket is still being generated - please check back in a moment.")
        return redirect('profile')

    ticket.pdf_file.open('rb')
    response = HttpResponse(ticket.pdf_file.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{ticket.booking_reference}.pdf"'
    ticket.pdf_file.close()
    return response


def verify_ticket(request, booking_reference):
    """Public verification page a theater staff member reaches by scanning
    the QR code on the printed/emailed ticket - confirms the booking is
    real and shows what it covers, without exposing the customer's payment
    details or requiring them to be logged in."""
    from .models import Ticket

    ticket = Ticket.objects.select_related('payment__theater__movie').filter(
        booking_reference=booking_reference
    ).first()

    if ticket is None:
        return render(request, 'payments/verify_ticket.html', {'valid': False}, status=404)

    payment = ticket.payment
    return render(request, 'payments/verify_ticket.html', {
        'valid': True,
        'booking_reference': ticket.booking_reference,
        'movie': payment.theater.movie,
        'theater': payment.theater,
        'seats': payment.seat_numbers,
        'payment_status': payment.status,
    })
