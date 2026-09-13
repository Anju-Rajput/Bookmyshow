from django.db import models


class DashboardAccess(models.Model):
    """This model has no table/columns of its own - it exists purely so we
    can attach custom Django permissions to something. That lets an admin
    grant "view_dashboard" / "export_dashboard_reports" to specific staff
    users individually (Admin -> Users -> permissions), instead of every
    is_staff account automatically getting analytics access.
    """

    class Meta:
        managed = False  # no CREATE TABLE - this is a permissions-only model
        default_permissions = ()  # skip Django's auto add/change/delete/view perms
        permissions = [
            ('view_dashboard', 'Can view analytics dashboard'),
            ('export_dashboard_reports', 'Can export analytics dashboard reports as CSV'),
        ]
