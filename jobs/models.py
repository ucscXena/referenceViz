from django.db import models
from django.contrib.auth.models import User
import uuid


class Reference(models.Model):
    """A reference dataset for cell-type projection. Admin-managed."""
    id = models.CharField(max_length=100, primary_key=True)  # URL-safe, e.g. 'Siletti_MSN'
    name = models.CharField(max_length=255)
    s3_uri = models.CharField(max_length=500)  # s3://bucket/references/<id>
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


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
        return str(self.id)[:5]

    def short_uploaded_file(self):
        return self.original_filename or 'unknown'

    def uce_s3_uri(self):
        """UCE embedding URI — supports old ('s3_uri') and new ('uce_s3_uri') key names."""
        if self.result:
            return self.result.get('uce_s3_uri') or self.result.get('s3_uri')
        return None

    def cell_count(self):
        return self.result.get('cell_count') if self.result else None


class Projection(models.Model):
    """One projection of a Job's UCE embedding into a Reference space."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('complete', 'Complete'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='projections')
    reference = models.ForeignKey(Reference, on_delete=models.CASCADE, related_name='projections')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    batch_job_id = models.CharField(max_length=255, blank=True)
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['reference__name']
        unique_together = [('job', 'reference')]

    def __str__(self):
        return f"Projection {self.job_id} → {self.reference_id}"
