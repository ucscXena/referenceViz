import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0003_sagemaker_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='Reference',
            fields=[
                ('id', models.CharField(max_length=100, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('s3_uri', models.CharField(max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='Projection',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('running', 'Running'), ('complete', 'Complete'), ('error', 'Error')],
                    default='pending',
                    max_length=20,
                )),
                ('batch_job_id', models.CharField(blank=True, max_length=255)),
                ('result', models.JSONField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('job', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='projections',
                    to='jobs.job',
                )),
                ('reference', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='projections',
                    to='jobs.reference',
                )),
            ],
            options={
                'ordering': ['reference__name'],
                'unique_together': {('job', 'reference')},
            },
        ),
    ]
