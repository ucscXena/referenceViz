from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.contrib import admin
from django.db.models import Count, Q
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .aws import boto_client
from .models import Job, JobEvent, Projection, ProjectionEvent, Reference, ReferenceGroup, UCEModel


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


class ProjectionInline(admin.TabularInline):
    model = Projection
    extra = 0
    fields = ('reference', 'status', 'batch_job_link', 'download_link', 'created_at', 'updated_at')
    readonly_fields = ('reference', 'status', 'batch_job_link', 'download_link', 'created_at', 'updated_at')

    def batch_job_link(self, obj):
        return _batch_link(obj.batch_job_id)
    batch_job_link.short_description = 'Batch Job'

    def download_link(self, obj):
        s3_uri = obj.result.get('s3_uri') if obj.result else None
        return _presigned_link(s3_uri, 'Download')
    download_link.short_description = 'Download'


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ('short_id', 'user', 'original_filename', 'status', 'batch_job_link', 'created_at', 'uce_download_link')
    list_filter = ('status',)
    readonly_fields = ('id', 'batch_job_link', 'created_at', 'updated_at', 'uce_download_link')
    inlines = [ProjectionInline]
    change_list_template = 'admin/jobs/job/change_list.html'

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('stats/', self.admin_site.admin_view(self.stats_view), name='jobs_job_stats'),
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

    def short_id(self, obj):
        return str(obj.id)[:8]
    short_id.short_description = 'ID'
    short_id.admin_order_field = 'id'

    def batch_job_link(self, obj):
        return _batch_link(obj.batch_job_id)
    batch_job_link.short_description = 'Batch Job'
    batch_job_link.admin_order_field = 'batch_job_id'

    def uce_download_link(self, obj):
        return _presigned_link(obj.uce_s3_uri(), 'Download UCE')
    uce_download_link.short_description = 'UCE Embedding'


@admin.register(UCEModel)
class UCEModelAdmin(admin.ModelAdmin):
    list_display = ('name', 'model_url', 'is_default', 'created_at')
    readonly_fields = ('created_at',)


@admin.register(ReferenceGroup)
class ReferenceGroupAdmin(admin.ModelAdmin):
    list_display = ('title', 'default_version_link', 'created_at')
    readonly_fields = ('id', 'created_at')

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

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is not None:
            form.base_fields['default_version'].queryset = (
                Reference.objects.filter(group=obj)
            )
        return form


@admin.register(Reference)
class ReferenceAdmin(admin.ModelAdmin):
    list_display = ('id', 'group', 'uce_model', 'version_label', 'is_active', 's3_uri', 'created_at')
    list_filter = ('is_active', 'uce_model')
    search_fields = ('id', 'group__title')
    readonly_fields = ('created_at',)


@admin.register(Projection)
class ProjectionAdmin(admin.ModelAdmin):
    list_display = ('short_id', 'short_job', 'reference_link', 'status', 'batch_job_link', 'download_link', 'created_at')
    list_filter = ('status', 'reference')
    readonly_fields = ('id', 'job', 'reference', 'status', 'batch_job_link', 'result', 'download_link', 'predictions_download_link', 'viz_link', 'created_at', 'updated_at')

    def short_id(self, obj):
        return str(obj.id)[:8]
    short_id.short_description = 'ID'
    short_id.admin_order_field = 'id'

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
