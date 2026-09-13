from django.db import IntegrityError, transaction

from .models import Seat, Booking


def finalize_seat_bookings(user, theater, payment=None):
    """Convert `user`'s currently-held (reserved, non-expired) seats for
    `theater` into real Booking rows.

    Safe to call more than once for the same user/theater/payment (e.g. the
    browser's success callback AND a Razorpay webhook both firing for the
    same event): seats that are already booked are simply skipped, so this
    never creates a duplicate Booking. The DB-level OneToOne on
    Booking.seat is an additional hard backstop against double-booking.

    Runs inside a locked transaction (select_for_update) so this is also
    safe against a *different* user racing for the same seat at the same
    moment.

    Returns (booked_seat_numbers, unavailable_seat_numbers).
    """
    booked, unavailable = [], []
    with transaction.atomic():
        seats = Seat.objects.select_for_update().filter(theater=theater, reserved_by=user)
        for seat in seats:
            if seat.is_booked:
                # Idempotent re-entry: this seat was already turned into a
                # Booking by an earlier call (e.g. the webhook beat the
                # browser callback to it). Nothing more to do.
                continue
            if seat.is_reservation_expired:
                unavailable.append(seat.seat_number)
                continue
            try:
                Booking.objects.create(
                    user=user, seat=seat, movie=theater.movie, theater=theater, payment=payment
                )
            except IntegrityError:
                # Another process already booked this exact seat between our
                # read and write (shouldn't happen under the row lock above,
                # but the OneToOne constraint is a hard backstop regardless).
                unavailable.append(seat.seat_number)
                continue
            seat.is_booked = True
            seat.reserved_by = None
            seat.reserved_at = None
            seat.save(update_fields=['is_booked', 'reserved_by', 'reserved_at'])
            booked.append(seat.seat_number)
    return booked, unavailable
