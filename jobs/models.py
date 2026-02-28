from django.db import models
from django.contrib.auth.models import User
import uuid

class Job(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('complete', 'Complete'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='jobs')
    original_filename = models.CharField(max_length=255, blank=True)
    s3_input_key = models.CharField(max_length=500, blank=True)
    s3_output_key = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Job {self.id} for user {self.user.username} - Status: {self.status}"

    def short_id(self):
        """Return the first 5 characters of the UUID."""
        return str(self.id)[:5]

    def short_uploaded_file(self):
        return self.original_filename or 'unknown'
