import uuid

import django.db.models.deletion
from django.db import migrations, models


UCE_EXCLUDE_KEYS = {'upload_id', 'output_uri', 'failure_uri'}


def _forward(apps, schema_editor):
    Job = apps.get_model('jobs', 'Job')
    UCEEmbedding = apps.get_model('jobs', 'UCEEmbedding')
    Projection = apps.get_model('jobs', 'Projection')

    job_to_emb = {}
    for job in Job.objects.filter(uce_model_id__isnull=False):
        raw_result = job.result or {}
        emb_result = {k: v for k, v in raw_result.items() if k not in UCE_EXCLUDE_KEYS} or None

        # Determine embedding status from job status
        job_status = job.status
        if job_status in ('uploading', 'pending'):
            emb_status = 'pending'
        else:
            emb_status = job_status  # running / complete / error

        emb = UCEEmbedding.objects.create(
            job=job,
            uce_model_id=job.uce_model_id,
            batch_job_id=job.batch_job_id or '',
            status=emb_status,
            result=emb_result,
        )
        job_to_emb[job.pk] = emb

    # Backfill Projection.embedding
    for proj in Projection.objects.filter(job_id__in=job_to_emb):
        emb = job_to_emb.get(proj.job_id)
        if emb:
            proj.embedding = emb
            proj.save(update_fields=['embedding'])


def _backward(apps, schema_editor):
    # Best-effort: copy embedding state back onto job
    Job = apps.get_model('jobs', 'Job')
    UCEEmbedding = apps.get_model('jobs', 'UCEEmbedding')

    for emb in UCEEmbedding.objects.select_related('job'):
        job = emb.job
        job.uce_model_id = emb.uce_model_id
        job.batch_job_id = emb.batch_job_id or ''
        if emb.result:
            job.result = {**(job.result or {}), **emb.result}
        job.save(update_fields=['uce_model_id', 'batch_job_id', 'result'])


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0021_ucemodel_pipeline'),
    ]

    operations = [
        # 1. Create UCEEmbedding table
        migrations.CreateModel(
            name='UCEEmbedding',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('batch_job_id', models.CharField(blank=True, max_length=255)),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('running', 'Running'),
                             ('complete', 'Complete'), ('error', 'Error')],
                    default='pending', max_length=20)),
                ('result', models.JSONField(null=True, blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('job', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='embeddings',
                    to='jobs.job',
                )),
                ('uce_model', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='embeddings',
                    to='jobs.ucemodel',
                )),
            ],
            options={
                'unique_together': {('job', 'uce_model')},
            },
        ),
        # 2. Add Projection.embedding FK (nullable)
        migrations.AddField(
            model_name='projection',
            name='embedding',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='projections',
                to='jobs.uceembedding',
            ),
        ),
        # 3. Data migration: create UCEEmbedding rows and backfill Projection.embedding
        migrations.RunPython(_forward, _backward),
        # 4. Remove Job.uce_model and Job.batch_job_id
        migrations.RemoveField(model_name='job', name='uce_model'),
        migrations.RemoveField(model_name='job', name='batch_job_id'),
    ]
