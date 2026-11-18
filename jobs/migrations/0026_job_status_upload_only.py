"""Job.status now describes only the uploaded file; UCE state lives on UCEEmbedding.

Data changes:
- Job.status: pending/running/complete -> ready. error -> ready, unless the error
  is the job's own (a failed clone, with result['error'] and no failed embedding).
  pending with no embeddings (never confirmed) -> uploading.
- Example jobs, which predate UCEEmbedding and kept uce_s3_uri and cell_count in
  Job.result, get a complete UCEEmbedding and their projections point at it. They
  are also marked result['example'] = True, which is how they are found now.
- Projections left pending behind a failed embedding are marked failed, as
  UCEEmbedding.fail_projections() now does when an embedding fails.
"""
from django.db import migrations, models


def _forward(apps, schema_editor):
    Job = apps.get_model('jobs', 'Job')
    UCEEmbedding = apps.get_model('jobs', 'UCEEmbedding')
    UCEModel = apps.get_model('jobs', 'UCEModel')
    Projection = apps.get_model('jobs', 'Projection')

    default_model = UCEModel.objects.filter(is_default=True).first()

    for job in Job.objects.all():
        result = job.result or {}
        has_embeddings = UCEEmbedding.objects.filter(job=job).exists()

        if job.status == 'uploading':
            new = 'uploading'
        elif job.status == 'pending' and not has_embeddings:
            new = 'uploading'
        elif job.status == 'error':
            own_error = result.get('error') and not UCEEmbedding.objects.filter(
                job=job, status='error').exists()
            new = 'error' if own_error else 'ready'
        else:
            new = 'ready'
        if new != job.status:
            job.status = new
            job.save(update_fields=['status'])

        # Example jobs: UCE output recorded on the job itself, no embedding row.
        if not has_embeddings and result.get('uce_s3_uri'):
            proj = Projection.objects.filter(job=job).select_related('reference').first()
            uce_model_id = proj.reference.uce_model_id if proj else (
                default_model.id if default_model else None)
            if uce_model_id is None:
                continue
            emb_result = {'uce_s3_uri': result['uce_s3_uri']}
            if result.get('cell_count') is not None:
                emb_result['cell_count'] = result['cell_count']
            emb = UCEEmbedding.objects.create(
                job=job, uce_model_id=uce_model_id, status='complete', result=emb_result)
            Projection.objects.filter(job=job, embedding__isnull=True).update(embedding=emb)
            job.result = {**result, 'example': True}
            job.save(update_fields=['result'])

    for proj in Projection.objects.filter(status='pending', embedding__status='error').select_related('embedding'):
        error = (proj.embedding.result or {}).get('error') or 'Unknown error'
        proj.status = 'error'
        proj.result = {'error': f'UCE embedding failed: {error}', 'uce_failed': True}
        proj.save(update_fields=['status', 'result'])


def _backward(apps, schema_editor):
    Job = apps.get_model('jobs', 'Job')
    Job.objects.filter(status='ready').update(status='complete')
    Job.objects.filter(status='copying').update(status='pending')


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0025_publication_fulltext'),
    ]

    operations = [
        migrations.AlterField(
            model_name='job',
            name='status',
            field=models.CharField(
                choices=[('uploading', 'Uploading'), ('copying', 'Copying'),
                         ('ready', 'Ready'), ('error', 'Error')],
                default='uploading', max_length=20),
        ),
        migrations.RunPython(_forward, _backward),
    ]
