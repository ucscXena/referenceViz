import json
from collections import defaultdict
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.utils import quote
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join, mark_safe


class _S3KeyWidget(forms.TextInput):
    def __init__(self, download_url, *args, **kwargs):
        self.download_url = download_url
        super().__init__(*args, **kwargs)

    def render(self, name, value, attrs=None, renderer=None):
        html = super().render(name, value, attrs, renderer)
        return mark_safe(
            html + format_html(
                '<a href="{}" target="_blank" style="margin-left:10px; align-self:center; font-size:13px">↓ Download</a>',
                self.download_url,
            )
        )

from .aws import boto_client
from django.contrib.auth.admin import UserAdmin as DefaultUserAdmin
from django.contrib.auth.models import User

from .models import ConversationMessage, Job, JobEvent, Projection, ProjectionEvent, Publication, Reference, ReferenceGroup, UCEEmbedding, UCEModel, UserProfile
from .tasks import run_analysis


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'Notification preferences'
    fields = ('email_on_complete',)


class UserAdmin(DefaultUserAdmin):
    inlines = list(DefaultUserAdmin.inlines) + [UserProfileInline]


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


def _presigned_link(s3_uri, label):
    """Generate a presigned download link for an S3 URI, or return '—'."""
    if not s3_uri:
        return '—'
    try:
        bucket, key = s3_uri.replace('s3://', '').split('/', 1)
        url = boto_client('s3').generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': key},
            ExpiresIn=300,
        )
        return format_html('<a href="{}">{}</a>', url, label)
    except Exception:
        return s3_uri


def _batch_link(batch_job_id):
    """Return a linked short batch job ID, or '—'."""
    if not batch_job_id:
        return '—'
    label = str(batch_job_id)[:8]
    url = settings.AWS_BATCH_CONSOLE_URL.format(batch_job_id)
    return format_html('<a href="{}" target="_blank">{}</a>', url, label)


class UCEEmbeddingInline(admin.TabularInline):
    model = UCEEmbedding
    extra = 0
    fields = ('link', 'uce_model', 'status', 'uce_download_link', 'created_at', 'updated_at')
    readonly_fields = ('link', 'uce_model', 'status', 'uce_download_link', 'created_at', 'updated_at')

    def link(self, obj):
        url = reverse('admin:jobs_uceembedding_change', args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, str(obj.id)[:8])
    link.short_description = 'ID'

    def uce_download_link(self, obj):
        return _presigned_link(obj.uce_s3_uri(), 'Download')
    uce_download_link.short_description = 'UCE Embedding'


class ProjectionInline(admin.TabularInline):
    model = Projection
    extra = 0
    fields = ('link', 'status', 'batch_job_link', 'download_link', 'created_at', 'updated_at')
    readonly_fields = ('link', 'status', 'batch_job_link', 'download_link', 'created_at', 'updated_at')

    def link(self, obj):
        url = reverse('admin:jobs_projection_change', args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.short_id())
    link.short_description = 'ID'  # column header label

    def batch_job_link(self, obj):
        return _batch_link(obj.batch_job_id)
    batch_job_link.short_description = 'Batch Job'

    def download_link(self, obj):
        s3_uri = obj.result.get('s3_uri') if obj.result else None
        return _presigned_link(s3_uri, 'Download')
    download_link.short_description = 'Download'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'job',
            'reference__group',
        )



@admin.action(description='Retry UCE pipeline')
def retry_uce_action(modeladmin, request, queryset):
    retried = 0
    skipped = 0
    for job in queryset:
        errored = list(job.embeddings.filter(status='error'))
        if not errored or not job.s3_input_key:
            skipped += 1
            continue
        for embedding in errored:
            embedding.status = 'pending'
            embedding.batch_job_id = ''
            embedding.result = {}
            embedding.save()
            run_analysis.delay(str(embedding.id))
        job.sync_status()
        retried += 1
    modeladmin.message_user(request, f'Retried {retried} job(s); skipped {skipped} (no failed embedding or no upload).')


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ('short_id', 'user', 'filename_display', 'status', 'batch_job_link', 'created_at', 'uce_download_link')
    list_filter = ('status',)
    ordering = ('-created_at',)
    readonly_fields = ('id', 'batch_job_links', 'created_at', 'updated_at', 'uce_download_link', 'system_prompt_link')
    inlines = [UCEEmbeddingInline, ProjectionInline]
    actions = [retry_uce_action]
    change_list_template = 'admin/jobs/job/change_list.html'

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('stats/', self.admin_site.admin_view(self.stats_view), name='jobs_job_stats'),
            path('<uuid:job_id>/system-prompt/',
                 self.admin_site.admin_view(self.system_prompt_view),
                 name='jobs_job_system_prompt'),
        ]
        return custom + urls

    def stats_view(self, request):
        now = timezone.now()
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)

        # Live counts for currently active jobs
        job_live = Job.objects.aggregate(
            running=Count('id', filter=Q(status='running')),
            pending=Count('id', filter=Q(status='pending')),
        )

        # Historical counts from event log (survives job deletion)
        job_totals = JobEvent.objects.aggregate(
            total=Count('id', filter=Q(event='created')),
            complete=Count('id', filter=Q(event='complete')),
            error=Count('id', filter=Q(event='error')),
        )
        job_totals.update(job_live)

        # Jobs created per week (last 16 weeks) — from event log
        weeks = []
        for i in range(15, -1, -1):
            start = now - timedelta(weeks=i + 1)
            end = now - timedelta(weeks=i)
            count = JobEvent.objects.filter(event='created', timestamp__gte=start, timestamp__lt=end).count()
            weeks.append({'label': start.strftime('%-m/%-d'), 'count': count})

        # Active users from event log
        active_week = JobEvent.objects.filter(event='created', timestamp__gte=week_ago).values('user').distinct().count()
        active_month = JobEvent.objects.filter(event='created', timestamp__gte=month_ago).values('user').distinct().count()
        total_users = JobEvent.objects.filter(event='created').values('user').distinct().count()

        # Cell count distribution from event log
        cell_buckets = defaultdict(int)
        bucket_labels = ['<10k', '10k–50k', '50k–100k', '100k–250k', '250k–500k', '>500k']
        for n in JobEvent.objects.filter(event='complete').exclude(cell_count=None).values_list('cell_count', flat=True):
            if n < 10_000:
                cell_buckets['<10k'] += 1
            elif n < 50_000:
                cell_buckets['10k–50k'] += 1
            elif n < 100_000:
                cell_buckets['50k–100k'] += 1
            elif n < 250_000:
                cell_buckets['100k–250k'] += 1
            elif n < 500_000:
                cell_buckets['250k–500k'] += 1
            else:
                cell_buckets['>500k'] += 1
        cell_counts = [{'label': l, 'count': cell_buckets[l]} for l in bucket_labels]

        # Projections by reference from event log
        proj_by_ref = (
            ProjectionEvent.objects
            .values('reference_title', 'reference_id')
            .annotate(total=Count('id', filter=Q(event='created')),
                      complete=Count('id', filter=Q(event='complete')))
            .filter(total__gt=0)
            .order_by('-total')
        )

        context = {
            **self.admin_site.each_context(request),
            'title': 'Site Stats',
            'job_totals': job_totals,
            'weeks': weeks,
            'active_week': active_week,
            'active_month': active_month,
            'total_users': total_users,
            'cell_counts': cell_counts,
            'proj_by_ref': proj_by_ref,
        }
        return render(request, 'admin/jobs/job/stats.html', context)

    def system_prompt_view(self, request, job_id):
        from .chat import _build_system_prompt
        from .projection_summary import compute_projection_summary
        job = get_object_or_404(Job, pk=job_id)

        recomputed = 0
        if request.method == 'POST':
            for proj in job.projections.all():
                if proj.result and proj.result.get('s3_uri'):
                    proj.result.pop('summary', None)
                    proj.result.pop('column_notes', None)
                    try:
                        proj.result['summary'] = compute_projection_summary(proj.result['s3_uri'])
                    except Exception:
                        logger.exception('Failed to recompute summary for projection %s', proj.pk)
                    proj.save(update_fields=['result'])
                    recomputed += 1

        prompt = _build_system_prompt(job, chunks=[])
        context = {
            **self.admin_site.each_context(request),
            'title': f'System prompt — {job.original_filename or job.pk}',
            'prompt': prompt,
            'recomputed': recomputed if request.method == 'POST' else None,
        }
        return render(request, 'admin/jobs/job/system_prompt.html', context)

    def system_prompt_link(self, obj):
        if not obj.pk:
            return '—'
        url = reverse('admin:jobs_job_system_prompt', args=[obj.pk])
        return format_html('<a href="{}" target="_blank">View system prompt</a>', url)
    system_prompt_link.short_description = 'System Prompt'

    def filename_display(self, obj):
        name = obj.original_filename or '—'
        # Insert word-break opportunities after _, -, and . so long names wrap gracefully.
        broken = name.replace('_', '_<wbr>').replace('-', '-<wbr>').replace('.', '.<wbr>')
        return mark_safe(f'<span style="word-break:break-word">{broken}</span>')
    filename_display.short_description = 'File'
    filename_display.admin_order_field = 'original_filename'

    def batch_job_link(self, obj):
        # List view: show the most recent active embedding's batch job.
        emb = obj.embeddings.order_by('-created_at').first()
        if not emb:
            return '—'
        result = emb.result or {}
        if result.get('sharded'):
            job_id = result.get('merge_batch_job_id') or result.get('preprocess_batch_job_id')
            prefix = 'merge' if result.get('merge_batch_job_id') else 'pre'
            link = _batch_link(job_id)
            return mark_safe(f'{prefix}: {link}') if job_id else '—'
        return _batch_link(emb.batch_job_id)
    batch_job_link.short_description = 'Batch Job'

    def batch_job_links(self, obj):
        # Detail view: show all embeddings' batch job links.
        parts = []
        for emb in obj.embeddings.order_by('-created_at'):
            result = emb.result or {}
            if result.get('sharded'):
                preprocess_id = result.get('preprocess_batch_job_id')
                merge_id = result.get('merge_batch_job_id')
                if preprocess_id:
                    parts.append(f'pre: {_batch_link(preprocess_id)}')
                if merge_id:
                    parts.append(f'merge: {_batch_link(merge_id)}')
            elif emb.batch_job_id:
                parts.append(f'{emb.uce_model.name}: {_batch_link(emb.batch_job_id)}')
        return mark_safe(' · '.join(parts)) if parts else '—'
    batch_job_links.short_description = 'Batch Job'

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj and obj.s3_input_key:
            try:
                url = boto_client('s3').generate_presigned_url(
                    'get_object',
                    Params={'Bucket': settings.AWS_S3_BUCKET, 'Key': obj.s3_input_key},
                    ExpiresIn=300,
                )
                form.base_fields['s3_input_key'].widget = _S3KeyWidget(download_url=url)
            except Exception:
                pass
        return form

    def uce_download_link(self, obj):
        return _presigned_link(obj.uce_s3_uri(), 'Download UCE')
    uce_download_link.short_description = 'UCE Embedding'


@admin.register(UCEModel)
class UCEModelAdmin(admin.ModelAdmin):
    list_display = ('name', 'model_url', 'is_default', 'created_at')
    readonly_fields = ('created_at',)


class _FileLoadTextarea(forms.Textarea):
    """Textarea with a button that fills it from a local file, for wranglers whose
    scripts write these fields to files."""
    accept = '.txt,.md,text/plain,text/markdown'
    file_format = 'text'

    class Media:
        js = ('jobs/js/admin/textarea-file.js',)

    def render(self, name, value, attrs=None, renderer=None):
        html = super().render(name, value, attrs, renderer)
        textarea_id = (attrs or {}).get('id') or self.attrs.get('id') or f'id_{name}'
        # One wrapper, so the admin's flex row puts the controls under the textarea.
        return format_html(
            '<div class="textarea-file" style="flex:1">{}'
            '<div style="margin-top:6px">'
            '<label class="button" style="cursor:pointer; display:inline-block; padding:6px 12px">Load from file…'
            '<input type="file" accept="{}" data-load-into="{}" data-format="{}" hidden></label>'
            '<span class="textarea-file-status help" style="margin-left:10px; padding:0"></span>'
            '</div></div>',
            html, self.accept, textarea_id, self.file_format,
        )


class _PrettyJsonWidget(_FileLoadTextarea):
    accept = '.json,application/json'
    file_format = 'json'

    def format_value(self, value):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):  # empty, or invalid input being redisplayed
            return value or ''
        return '' if parsed is None else json.dumps(parsed, indent=2)


class UCEEmbeddingForm(forms.ModelForm):
    result = forms.JSONField(
        widget=_PrettyJsonWidget(attrs={
            'rows': 20,
            'style': 'font-family: monospace; font-size: 12px; width: 100%;',
        }),
        required=False,
    )

    class Meta:
        model = UCEEmbedding
        fields = '__all__'


@admin.register(UCEEmbedding)
class UCEEmbeddingAdmin(admin.ModelAdmin):
    form = UCEEmbeddingForm
    list_display = ('short_id', 'job_link', 'uce_model', 'status', 'batch_job_link',
                    'cell_count_display', 'created_at', 'uce_download_link')
    list_filter = ('status', 'uce_model')
    search_fields = ('job__original_filename', 'job__user__username')
    ordering = ('-created_at',)
    readonly_fields = ('id', 'job', 'uce_model', 'batch_job_id',
                       'batch_job_link', 'uce_download_link', 'created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('id', 'job', 'uce_model', 'status')}),
        ('Batch', {'fields': ('batch_job_id', 'batch_job_link')}),
        ('Result', {'fields': ('result', 'uce_download_link')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at')}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('job__user', 'uce_model')

    def short_id(self, obj):
        return str(obj.id)[:8]
    short_id.short_description = 'ID'
    short_id.admin_order_field = 'id'

    def job_link(self, obj):
        label = f'{str(obj.job_id)[:8]} · {obj.job.original_filename or obj.job.user.username}'
        url = reverse('admin:jobs_job_change', args=[obj.job_id])
        return format_html('<a href="{}">{}</a>', url, label)
    job_link.short_description = 'Job'
    job_link.admin_order_field = 'job'

    def batch_job_link(self, obj):
        result = obj.result or {}
        if result.get('sharded'):
            job_id = result.get('merge_batch_job_id') or result.get('preprocess_batch_job_id')
            prefix = 'merge' if result.get('merge_batch_job_id') else 'pre'
            link = _batch_link(job_id)
            return mark_safe(f'{prefix}: {link}') if job_id else '—'
        return _batch_link(obj.batch_job_id)
    batch_job_link.short_description = 'Batch Job'

    def cell_count_display(self, obj):
        n = obj.cell_count()
        return f'{n:,}' if n else '—'
    cell_count_display.short_description = 'Cells'

    def uce_download_link(self, obj):
        return _presigned_link(obj.uce_s3_uri(), 'Download')
    uce_download_link.short_description = 'UCE Embedding'


class ReferenceForm(forms.ModelForm):
    metadata = forms.JSONField(
        widget=_PrettyJsonWidget(attrs={
            'rows': 25,
            'style': 'font-family: monospace; font-size: 12px; width: 100%;',
        }),
        required=False,
        help_text='Full metadata JSON blob from UCSC_Brain_Explorer_metadata.json.',
    )
    image_file = forms.FileField(
        required=False,
        label='Upload new image',
        help_text='Upload a webp/png/jpg to replace the current image. Leave blank to keep existing.',
    )

    class Meta:
        model = Reference
        fields = '__all__'
        widgets = {
            'wrangling_notes': _FileLoadTextarea(attrs={'rows': 8}),
        }


@admin.register(ReferenceGroup)
class ReferenceGroupAdmin(admin.ModelAdmin):
    list_display = ('title', 'display_order', 'default_version_link', 'created_at')
    readonly_fields = ('id', 'created_at')
    fields = ('id', 'title', 'display_order', 'default_version', 'created_at')

    def default_version_link(self, obj):
        ref = obj.default_version
        if not ref:
            return '—'
        label = str(ref.id).split('-')[0]
        if ref.version_label:
            label += f' ({ref.version_label})'
        url = reverse('admin:jobs_reference_change', args=[ref.pk])
        return format_html('<a href="{}">{}</a>', url, label)
    default_version_link.short_description = 'Default Version'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('default_version').defer(
            *(f'default_version__{field}' for field in Reference.CONTENT_FIELDS))

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is not None:
            form.base_fields['default_version'].queryset = Reference.objects.filter(group=obj)
        return form


@admin.register(Reference)
class ReferenceAdmin(admin.ModelAdmin):
    form = ReferenceForm
    list_display = ('id', 'group', 'uce_model', 'version_label', 'is_active', 's3_uri',
                    'has_metadata', 'has_image', 'created_at', 'map_link')
    list_filter = ('is_active', 'uce_model')
    search_fields = ('id', 'group__title')
    readonly_fields = ('crossref_status', 'current_image', 'created_at')
    change_list_template = 'admin/jobs/reference/change_list.html'

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('sync-prod/', self.admin_site.admin_view(self.sync_prod_view),
                 name='jobs_reference_sync_prod'),
        ]
        return custom + urls

    def sync_prod_view(self, request):
        from io import StringIO
        from django.core.management import call_command
        from django.db import connections

        has_prod = 'production' in connections

        src_only, dst_only = [], []
        if has_prod:
            with connections['default'].cursor() as c:
                c.execute('SELECT app, name FROM django_migrations ORDER BY app, name')
                src_migrations = set(tuple(r) for r in c.fetchall())
            with connections['production'].cursor() as c:
                c.execute('SELECT app, name FROM django_migrations ORDER BY app, name')
                dst_migrations = set(tuple(r) for r in c.fetchall())
            src_only = sorted(src_migrations - dst_migrations)
            dst_only = sorted(dst_migrations - src_migrations)

        output = None
        error = None

        if request.method == 'POST':
            if not has_prod:
                error = 'No production database configured — add a "production" entry to DATABASES in site_settings_private.py.'
            elif (src_only or dst_only) and not request.POST.get('override'):
                error = 'Schema mismatch between dev and prod — check the override box to proceed anyway.'
            else:
                out = StringIO()
                try:
                    call_command('sync_reference_data', stdout=out, stderr=out)
                    output = out.getvalue().strip()
                except Exception as e:
                    import traceback
                    error = f'{e}\n\n{traceback.format_exc()}'

        context = {
            **self.admin_site.each_context(request),
            'title': 'Sync reference catalog dev → prod',
            'has_prod': has_prod,
            'schema_mismatch': bool(src_only or dst_only),
            'src_only': src_only,
            'dst_only': dst_only,
            'output': output,
            'error': error,
        }
        return render(request, 'admin/jobs/reference/sync_prod.html', context)
    fieldsets = (
        (None, {'fields': ('id', 'group', 'uce_model', 's3_uri', 'version_label', 'notes',
                           'is_active', 'created_at')}),
        ('Metadata', {'fields': ('metadata', 'crossref_status')}),
        ('Image', {'fields': ('current_image', 'image_file')}),
        ('Wrangling Notes', {'fields': ('wrangling_notes',)}),
    )

    @admin.display(description='')
    def map_link(self, obj):
        return format_html('<a href="/jobs/references/?ref={}">Map</a>', obj.pk)

    @admin.display(boolean=True, description='Metadata')
    def has_metadata(self, obj):
        return bool(obj.metadata)

    @admin.display(boolean=True, description='Image')
    def has_image(self, obj):
        return bool(obj.image)

    @admin.display(description='Crossref status')
    def crossref_status(self, obj):
        dois = obj.cited_dois()
        if not dois:
            return 'No DOIs in metadata'
        rows = {p.doi: p for p in Publication.objects.filter(doi__in=dois)}
        fetched = [d for d in dois if d in rows and not rows[d].fetch_error]
        failed = [d for d in dois if d in rows and rows[d].fetch_error]
        pending = len(dois) - len(fetched) - len(failed)
        parts = [format_html('{}/{} publications fetched', len(fetched), len(dois))]
        if pending:
            parts.append(format_html('{} waiting to be fetched', pending))
        if failed:
            links = format_html_join(', ', '<a href="{}"><code>{}</code></a>', (
                (reverse('admin:jobs_publication_change', args=[quote(d)]), d) for d in failed))
            parts.append(format_html('<span style="color:#c00">failed: {}</span>', links))
        return format_html_join('; ', '{}', ((part,) for part in parts))

    def current_image(self, obj):
        if not obj.image:
            return '(none)'
        url = reverse('reference_image', args=[obj.pk])
        return format_html(
            '<img src="{}" style="max-width:220px; max-height:160px; border:1px solid #ddd; border-radius:4px;">',
            url,
        )
    current_image.short_description = 'Current Image'

    def save_model(self, request, obj, form, change):
        image_file = form.cleaned_data.get('image_file')
        if image_file:
            obj.image = image_file.read()
            obj.image_content_type = image_file.content_type or 'image/webp'

        super().save_model(request, obj, form, change)


@admin.register(Publication)
class PublicationAdmin(admin.ModelAdmin):
    """Crossref details by DOI. Filled in automatically when a reference citing the
    DOI is saved; editable for a DOI Crossref can't resolve."""
    list_display = ('doi', 'title', 'year', 'fetch_error', 'fetched_at')
    list_filter = (('fetch_error', admin.EmptyFieldListFilter),)
    search_fields = ('doi', 'title', 'authors')
    readonly_fields = ('fetched_at',)
    actions = ('fetch_again',)

    @admin.action(description='Fetch selected publications again from Crossref')
    def fetch_again(self, request, queryset):
        from .tasks import fetch_crossref_publications
        dois = list(queryset.values_list('doi', flat=True))
        run = fetch_crossref_publications if getattr(settings, 'RQSYNC', False) else fetch_crossref_publications.delay
        run(dois)
        self.message_user(request, f'Fetching {len(dois)} publication(s) from Crossref.')


@admin.register(Projection)
class ProjectionAdmin(admin.ModelAdmin):
    list_display = ('short_id', 'short_job', 'reference_link', 'status', 'batch_job_link', 'download_link', 'created_at')
    list_filter = ('status', 'reference')
    ordering = ('-created_at',)
    readonly_fields = ('id', 'job_file', 'job', 'reference', 'status', 'batch_job_link', 'result', 'download_link', 'predictions_download_link', 'viz_link', 'created_at', 'updated_at')
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('reference__group', 'job__user').defer(
            *(f'reference__{field}' for field in Reference.CONTENT_FIELDS))

    def job_file(self, obj):
        return obj.job.original_filename or '—'
    job_file.short_description = 'Input file'

    def short_job(self, obj):
        label = f'{str(obj.job_id)[:8]} · {obj.job.user.username}'
        url = reverse('admin:jobs_job_change', args=[obj.job_id])
        return format_html('<a href="{}">{}</a>', url, label)
    short_job.short_description = 'Job'
    short_job.admin_order_field = 'job'

    def reference_link(self, obj):
        ref = obj.reference
        label = str(ref.id).split('-')[0]
        if ref.version_label:
            label += f' ({ref.version_label})'
        url = reverse('admin:jobs_reference_change', args=[ref.pk])
        return format_html('<a href="{}">{}</a>', url, label)
    reference_link.short_description = 'Reference'
    reference_link.admin_order_field = 'reference'

    def batch_job_link(self, obj):
        return _batch_link(obj.batch_job_id)
    batch_job_link.short_description = 'Batch Job'
    batch_job_link.admin_order_field = 'batch_job_id'

    def download_link(self, obj):
        s3_uri = obj.result.get('s3_uri') if obj.result else None
        return _presigned_link(s3_uri, 'Download')
    download_link.short_description = 'Result (arrow)'

    def predictions_download_link(self, obj):
        s3_uri = obj.result.get('predictions_s3_uri') if obj.result else None
        return _presigned_link(s3_uri, 'Download')
    predictions_download_link.short_description = 'Predictions (tsv)'

    def viz_link(self, obj):
        s3_uri = obj.result.get('s3_uri') if obj.result else None
        if not s3_uri:
            return '—'
        from urllib.parse import quote
        url = f'/visualization/{obj.reference_id}/?overlay={quote(s3_uri, safe="")}'
        return format_html('<a href="{}" target="_blank">Open visualization</a>', url)
    viz_link.short_description = 'Visualization'


@admin.register(ConversationMessage)
class ConversationMessageAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'job_link', 'generation', 'role', 'hidden', 'content_preview')
    list_filter = ('role', 'hidden', 'generation')
    readonly_fields = ('job', 'generation', 'role', 'hidden', 'content', 'charts', 'suggestions', 'created_at')
    ordering = ('-created_at',)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('job__user')

    def job_link(self, obj):
        label = f'{str(obj.job_id)[:8]} · {obj.job.user.username}'
        url = reverse('admin:jobs_job_change', args=[obj.job_id])
        return format_html('<a href="{}">{}</a>', url, label)
    job_link.short_description = 'Job'
    job_link.admin_order_field = 'job'

    def content_preview(self, obj):
        return obj.content[:80] + '…' if len(obj.content) > 80 else obj.content
    content_preview.short_description = 'Content'
