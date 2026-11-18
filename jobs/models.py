import uuid

from django.contrib.auth.models import User
from django.db import models
from pgvector.django import VectorField


def normalize_doi(doi):
    """Normalize a DOI to lowercase. DOIs are case-insensitive; lowercase is the canonical form."""
    return doi.strip().lower() if doi else doi



class UCEModel(models.Model):
    """A UCE embedding model version. Admin-managed; one row should have is_default=True."""

    PIPELINE_UCE       = 'uce'
    PIPELINE_UCE_BRAIN = 'uce-brain'
    PIPELINE_CHOICES = [
        (PIPELINE_UCE,       'UCE'),
        (PIPELINE_UCE_BRAIN, 'UCE Brain'),
    ]

    name = models.CharField(max_length=100, unique=True)
    model_url = models.CharField(max_length=500)  # passed as model_s3 to Batch
    pipeline = models.CharField(max_length=20, choices=PIPELINE_CHOICES,
                                default=PIPELINE_UCE)
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
    display_order = models.PositiveIntegerField(
        default=0, help_text='Position on the public references page; lower values come first.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['title']

    def __str__(self):
        return self.title


class Publication(models.Model):
    """
    Crossref details for a DOI cited in Reference.metadata['publication'].

    Kept apart from the metadata so that the metadata stays exactly what the
    wranglers wrote; pages join the two by DOI. Rows are shared by every
    reference that cites the DOI, and are fetched when a reference citing a DOI
    without a row (or with a failed fetch) is saved.
    """
    FULLTEXT_STATUS_NONE     = 'none'
    FULLTEXT_STATUS_FETCHING = 'fetching'
    FULLTEXT_STATUS_COMPLETE = 'complete'
    FULLTEXT_STATUS_ERROR    = 'error'
    FULLTEXT_STATUS_CHOICES  = [
        ('none',     'Not fetched'),
        ('fetching', 'Fetching'),
        ('complete', 'Complete'),
        ('error',    'Error'),
    ]

    doi = models.CharField(max_length=255, primary_key=True)
    title = models.TextField(blank=True)
    authors = models.TextField(blank=True)
    journal = models.TextField(blank=True)
    year = models.CharField(max_length=10, blank=True)
    fetch_error = models.TextField(blank=True)  # set when the last Crossref fetch failed
    fetched_at = models.DateTimeField(null=True, blank=True)
    fulltext = models.TextField(blank=True)
    fulltext_source = models.CharField(max_length=20, blank=True)  # pmc_oa / biorxiv_s3 / unpaywall_pdf / manual / abstract
    fulltext_status = models.CharField(max_length=10, choices=FULLTEXT_STATUS_CHOICES, default='none')
    fulltext_error = models.TextField(blank=True)

    class Meta:
        ordering = ['doi']

    def __str__(self):
        return self.doi


class Reference(models.Model):
    """A specific build of a reference dataset. Admin-managed."""
    # Large columns; defer these in queries that list references.
    CONTENT_FIELDS = ('metadata', 'wrangling_notes', 'image')

    id = models.CharField(max_length=100, primary_key=True)  # URL-safe, e.g. 'Siletti_MSN'
    group = models.ForeignKey(ReferenceGroup, on_delete=models.PROTECT, related_name='versions')
    uce_model = models.ForeignKey(UCEModel, on_delete=models.PROTECT, related_name='references')
    s3_uri = models.CharField(max_length=500)  # s3://bucket/references/<id>
    version_label = models.CharField(max_length=50, blank=True)  # e.g. 'v2', '2025-09'
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(null=True, blank=True)
    wrangling_notes = models.TextField(blank=True)
    image = models.BinaryField(null=True, blank=True)
    image_content_type = models.CharField(max_length=50, blank=True, default='image/webp')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['group__title']

    @property
    def name(self):
        return self.group.title

    def __str__(self):
        return f'{self.group.title}' + (f' ({self.version_label})' if self.version_label else '')

    def cited_dois(self):
        """DOIs of the publications in this reference's metadata, in order, without repeats."""
        dois = (p.get('doi') for p in (self.metadata or {}).get('publication') or [])
        return list(dict.fromkeys(normalize_doi(d) for d in dois if d))


class Job(models.Model):
    """One uploaded file. UCE results live on its UCEEmbeddings, mappings on its Projections.

    status describes only the input file: 'uploading' until the upload is confirmed,
    'copying' while a clone's files are copied, then 'ready'. 'error' means the input
    never arrived (truncated upload, failed clone), with the message in result['error'].
    """
    STATUS_CHOICES = [
        ('uploading', 'Uploading'),
        ('copying', 'Copying'),
        ('ready', 'Ready'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='jobs')
    original_filename = models.CharField(max_length=255, blank=True)
    s3_input_key = models.CharField(max_length=500, blank=True)
    s3_output_key = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='uploading')
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
        """UCE embedding URI from the most relevant embedding."""
        emb = self.embeddings.filter(status='complete').order_by('created_at').first()
        if emb is None:
            emb = self.embeddings.order_by('created_at').first()
        return emb.uce_s3_uri() if emb else None

    def cell_count(self):
        emb = self.embeddings.filter(status='complete').order_by('created_at').first()
        return emb.cell_count() if emb else None


class UCEEmbedding(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('complete', 'Complete'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='embeddings')
    uce_model = models.ForeignKey(UCEModel, on_delete=models.PROTECT, related_name='embeddings')
    batch_job_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('job', 'uce_model')]

    def __str__(self):
        return f'UCEEmbedding {str(self.id)[:8]} for Job {str(self.job_id)[:8]} ({self.status})'

    def uce_s3_uri(self):
        if self.result:
            return self.result.get('uce_s3_uri') or self.result.get('s3_uri')
        return None

    def cell_count(self):
        return self.result.get('cell_count') if self.result else None

    # Projections wait in 'pending' until their embedding completes. These keep them in
    # step with the embedding, so a failed embedding never leaves them pending forever.

    def fail_projections(self):
        """Mark projections waiting on this embedding as failed. Call after it fails."""
        error = (self.result or {}).get('error') or 'Unknown error'
        for proj in self.projections.filter(status='pending'):
            proj.status = 'error'
            proj.result = {'error': f'UCE embedding failed: {error}', 'uce_failed': True}
            proj.save()

    def projections_to_submit(self):
        """Projections to submit now that this embedding is complete, including any it had failed."""
        self._reopen_projections()
        return list(self.projections.filter(status='pending'))

    def reset_for_retry(self):
        """Return this embedding, and the projections it failed, to pending. The caller enqueues run_analysis."""
        self.status = 'pending'
        self.batch_job_id = ''
        self.result = {}
        self.save()
        self._reopen_projections()

    def _reopen_projections(self):
        for proj in self.projections.filter(status='error', result__uce_failed=True):
            proj.status = 'pending'
            proj.result = {}
            proj.save()


class JobEvent(models.Model):
    """Permanent record of job lifecycle events, surviving job deletion.

    'created': a Job row was created (uploads, including abandoned ones, clones and
    example jobs). 'uploaded': an upload finished (uploading -> ready). 'error': an
    upload or clone failed. 'complete' is legacy: before Job.status described only the
    upload (Nov 2026), it meant the job's UCE run finished, with its cell count; UCE
    runs are now recorded as EmbeddingEvents.
    """
    EVENT_CHOICES = [('created', 'Created'), ('uploaded', 'Uploaded'),
                     ('complete', 'Complete (legacy)'), ('error', 'Error')]

    job_id = models.UUIDField(db_index=True)  # not a FK — survives job deletion
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='+')
    event = models.CharField(max_length=20, choices=EVENT_CHOICES)
    cell_count = models.IntegerField(null=True, blank=True)
    timestamp = models.DateTimeField()

    class Meta:
        ordering = ['timestamp']


class EmbeddingEvent(models.Model):
    """Permanent record of UCE runs, surviving job and embedding deletion.

    'started' each time an embedding starts running (so retries count), 'complete' or
    'error' when it finishes. Embeddings that are copied rather than run (clones,
    example jobs) record nothing.
    """
    EVENT_CHOICES = [('started', 'Started'), ('complete', 'Complete'), ('error', 'Error')]

    embedding_id = models.UUIDField(db_index=True)  # not a FK — survives deletion
    job_id = models.UUIDField(db_index=True)
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='+')
    uce_model_id = models.IntegerField()                 # denormalized — survives model changes
    uce_model_name = models.CharField(max_length=100)
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
    embedding = models.ForeignKey('UCEEmbedding', null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name='projections')
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


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    email_on_complete = models.BooleanField(default=True)

    def __str__(self):
        return f'Profile({self.user})'


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
