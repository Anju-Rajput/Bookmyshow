"""
Renders a booking's Payment (+ its Bookings/Seats/Theater/Movie) into a
professional-looking PDF ticket, with an embedded QR code for verification.

Kept as a pure function (Payment -> bytes) with no Celery/DB-write side
effects, so it's trivially unit-testable and reusable from both the async
email task and the synchronous "download my ticket" view.
"""
import io

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A5
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER


def _make_qr_image(data, box_size=6):
    qr = qrcode.QRCode(box_size=box_size, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


def build_ticket_pdf(payment, booking_reference, qr_verification_url):
    """Returns the ticket as raw PDF bytes. `payment` must have its
    `.seats` and `.theater.movie` reachable (a couple of small queries;
    this is called once per successful payment, not in a hot loop)."""
    theater = payment.theater
    movie = theater.movie
    seats = list(payment.seats.order_by('seat_number').values_list('seat_number', flat=True))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A5,
        topMargin=14 * mm, bottomMargin=14 * mm, leftMargin=14 * mm, rightMargin=14 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TicketTitle', parent=styles['Title'], fontSize=18, spaceAfter=2)
    sub_style = ParagraphStyle('TicketSub', parent=styles['Normal'], textColor=colors.grey, fontSize=10)
    center_style = ParagraphStyle('Center', parent=styles['Normal'], alignment=TA_CENTER, fontSize=9, textColor=colors.grey)

    elements = []
    elements.append(Paragraph('BookMySeat', title_style))
    elements.append(Paragraph('E-Ticket / Booking Confirmation', sub_style))
    elements.append(Spacer(1, 8 * mm))

    elements.append(Paragraph(movie.name, ParagraphStyle('Movie', parent=styles['Heading2'])))

    details_rows = [
        ['Theater / Screen', theater.name],
        ['Show Timing', theater.time.strftime('%d %b %Y, %I:%M %p')],
        ['Seats', ', '.join(seats) if seats else '-'],
        ['Booking ID', booking_reference],
        ['Payment Reference', payment.transaction_id],
        ['Amount Paid', f'INR {payment.amount}'],
    ]
    table = Table(details_rows, colWidths=[45 * mm, 75 * mm])
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 0), (-1, -2), 0.5, colors.lightgrey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 10 * mm))

    qr_buf = _make_qr_image(qr_verification_url)
    qr_img = RLImage(qr_buf, width=35 * mm, height=35 * mm)
    qr_img.hAlign = 'CENTER'
    elements.append(qr_img)
    elements.append(Spacer(1, 3 * mm))
    elements.append(Paragraph('Scan at the theater entrance to verify this ticket', center_style))
    elements.append(Spacer(1, 6 * mm))
    elements.append(Paragraph(
        'This is a computer-generated ticket and does not require a signature.',
        center_style,
    ))

    doc.build(elements)
    return buf.getvalue()
