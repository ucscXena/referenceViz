import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0005_projection_public'),
    ]

    operations = [
        migrations.CreateModel(
            name='UCEModel',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('model_url', models.CharField(max_length=500)),
                ('is_default', models.BooleanField(default=False)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'UCE Model',
            },
        ),
        migrations.CreateModel(
            name='ReferenceGroup',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('title', models.CharField(max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['title'],
            },
        ),
        # Add nullable FKs to Reference before ReferenceGroup.default_version,
        # so both tables exist when we add the cross-reference.
        migrations.AddField(
            model_name='reference',
            name='group',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='versions',
                to='jobs.referencegroup',
            ),
        ),
        migrations.AddField(
            model_name='reference',
            name='uce_model',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='references',
                to='jobs.ucemodel',
            ),
        ),
        migrations.AddField(
            model_name='reference',
            name='version_label',
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name='reference',
            name='is_active',
            field=models.BooleanField(default=True),
        ),
        # Now add the circular FK from ReferenceGroup back to Reference.
        migrations.AddField(
            model_name='referencegroup',
            name='default_version',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='jobs.reference',
            ),
        ),
        migrations.AddField(
            model_name='job',
            name='uce_model',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='jobs',
                to='jobs.ucemodel',
            ),
        ),
    ]
