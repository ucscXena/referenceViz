"""
Send a test email via the configured backend to verify SES is working.

Usage:
    python manage.py send_test_email bcraft@ucsc.edu
"""
from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Send a test email to verify the email backend (SES) is configured correctly'

    def add_arguments(self, parser):
        parser.add_argument('recipient', help='Email address to send to')

    def handle(self, *args, **options):
        recipient = options['recipient']
        self.stdout.write(f"Backend : {settings.EMAIL_BACKEND}")
        self.stdout.write(f"From    : {settings.DEFAULT_FROM_EMAIL}")
        self.stdout.write(f"To      : {recipient}")
        try:
            send_mail(
                subject='Test email — UCSC Brain Explorer',
                message=(
                    'This is a test message sent by the send_test_email management command.\n\n'
                    f'Backend: {settings.EMAIL_BACKEND}\n'
                    f'From:    {settings.DEFAULT_FROM_EMAIL}\n'
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient],
                fail_silently=False,
            )
            self.stdout.write(self.style.SUCCESS('Sent successfully.'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Failed: {e}'))
