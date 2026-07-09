import uuid

from django.contrib.auth.models import User
from django.db import models
from pgvector.django import VectorField



class UCEModel(models.Model):
    """A UCE embedding model version. Admin-managed; one row should have is_default=True."""
    name = models.CharField(max_length=100, unique=True)
    model_url = models.CharField(max_length=500)  # passed as model_s3 to Batch
    is_default = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'UCE Model'

    def __str__(self):
        return self.name


class ReferenceGroup(models.Model):
    """A conceptual reference dataset, grouping one or more versioned Reference builds."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    default_version = models.ForeignKey(
        'Reference', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['title']

    def __str__(self):
        return self.title


class Reference(models.Model):
    """A specific build of a reference dataset. Admin-managed."""
    id = models.CharField(max_length=100, primary_key=True)  # URL-safe, e.g. 'Siletti_MSN'
    group = models.ForeignKey(ReferenceGroup, on_delete=models.PROTECT, related_name='versions')
    uce_model = models.ForeignKey(UCEModel, on_delete=models.PROTECT, related_name='references')
    s3_uri = models.CharField(max_length=500)  # s3://bucket/references/<id>
    version_label = models.CharField(max_length=50, blank=True)  # e.g. 'v2', '2025-09'
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['group__title']

    @property
    def name(self):
        return self.group.title

    def __str__(self):
        return f'{self.group.title}' + (f' ({self.version_label})' if self.version_label else '')


class Job(models.Model):
    STATUS_CHOICES = [
        ('uploading', 'Uploading'),
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('complete', 'Complete'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='jobs')
    uce_model = models.ForeignKey(UCEModel, null=True, on_delete=models.PROTECT,
                                  related_name='jobs')
    batch_job_id = models.CharField(max_length=255, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    s3_input_key = models.CharField(max_length=500, blank=True)
    s3_output_key = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    result = models.JSONField(null=True, blank=True)
    current_conversation = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Job {self.id} for user {self.user.username} - Status: {self.status}"

    def short_id(self):
        return str(self.id)[:8]
    short_id.short_description = 'ID'
    short_id.admin_order_field = 'id'

    def short_uploaded_file(self):
        return self.original_filename or 'unknown'

    def uce_s3_uri(self):
        """UCE embedding URI — supports old ('s3_uri') and new ('uce_s3_uri') key names."""
        if self.result:
            return self.result.get('uce_s3_uri') or self.result.get('s3_uri')
        return None

    def cell_count(self):
        return self.result.get('cell_count') if self.result else None


class JobEvent(models.Model):
    """Permanent record of job lifecycle events, surviving job deletion."""
    EVENT_CHOICES = [('created', 'Created'), ('complete', 'Complete'), ('error', 'Error')]

    job_id = models.UUIDField(db_index=True)  # not a FK — survives job deletion
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='+')
    event = models.CharField(max_length=20, choices=EVENT_CHOICES)
    cell_count = models.IntegerField(null=True, blank=True)
    timestamp = models.DateTimeField()

    class Meta:
        ordering = ['timestamp']


class ProjectionEvent(models.Model):
    """Permanent record of projection lifecycle events, surviving job/projection deletion."""
    EVENT_CHOICES = [('created', 'Created'), ('complete', 'Complete'), ('error', 'Error')]

    projection_id = models.UUIDField(db_index=True)  # not a FK — survives deletion
    job_id = models.UUIDField(db_index=True)
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='+')
    reference_id = models.CharField(max_length=100)      # denormalized — survives reference changes
    reference_title = models.CharField(max_length=255)
    event = models.CharField(max_length=20, choices=EVENT_CHOICES)
    timestamp = models.DateTimeField()

    class Meta:
        ordering = ['timestamp']


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
    public = models.BooleanField(default=False)
    batch_job_id = models.CharField(max_length=255, blank=True)
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['reference__group__title']
        unique_together = [('job', 'reference')]

    def short_id(self):
        return str(self.id)[:8]
    short_id.short_description = 'ID'
    short_id.admin_order_field = 'id'

    def __str__(self):
        file = self.job.original_filename
        group_title = self.reference.group.title
        version = self.reference.version_label
        return f'{file} → {group_title} {version}'

class DocumentChunk(models.Model):
    """A chunk of text from a paper or reference metadata, with its embedding for RAG."""
    source_type = models.CharField(max_length=20)   # 'paper' | 'metadata'
    source_id = models.CharField(max_length=500)     # DOI or reference UUID
    source_label = models.CharField(max_length=500)  # human-readable label
    chunk_index = models.IntegerField()
    text = models.TextField()
    embedding = VectorField(dimensions=384)

    class Meta:
        ordering = ['source_id', 'chunk_index']

    def __str__(self):
        return f"{self.source_label} [{self.chunk_index}]"

class ShareToken(models.Model):
    """A time-limited token that lets another authenticated user clone a Job."""
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='share_tokens')
    token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'ShareToken for Job {str(self.job_id)[:8]} (expires {self.expires_at:%Y-%m-%d})'


class ConversationMessage(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='conversation_messages')
    generation = models.PositiveSmallIntegerField(default=0)
    role = models.CharField(max_length=10)   # 'user' | 'assistant'
    content = models.TextField()
    charts = models.JSONField(default=list)
    suggestions = models.JSONField(default=list)
    hidden = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'[{self.role}] Job {str(self.job_id)[:8]} gen={self.generation}'
