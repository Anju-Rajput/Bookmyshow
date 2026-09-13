from django.contrib import admin
from .models import Payment, Ticket


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        'razorpay_order_id', 'user', 'theater', 'amount', 'status',
        'transaction_id', 'retry_count', 'created_at',
    ]
    list_filter = ['status', 'theater']
    search_fields = ['user__username', 'razorpay_order_id', 'razorpay_payment_id']
    readonly_fields = [
        'user', 'theater', 'seats', 'amount', 'currency', 'razorpay_order_id',
        'razorpay_payment_id', 'razorpay_signature', 'created_at', 'updated_at',
    ]


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = [
        'booking_reference', 'payment', 'email_status', 'email_attempts',
        'generated_at', 'emailed_at',
    ]
    list_filter = ['email_status']
    search_fields = ['booking_reference', 'payment__user__username']
    readonly_fields = ['payment', 'booking_reference', 'pdf_file', 'qr_verification_url', 'generated_at']
    actions = ['resend_email']

    def resend_email(self, request, queryset):
        from .tasks import send_ticket_email
        for ticket in queryset:
            send_ticket_email.delay(ticket.id)
        self.message_user(request, f'Queued {queryset.count()} ticket email(s) for resend.')
    resend_email.short_description = 'Resend ticket email (async)'
