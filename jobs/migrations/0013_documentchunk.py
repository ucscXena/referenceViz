"""
Before running this migration, enable pgvector on the database as a superuser:

    CREATE EXTENSION IF NOT EXISTS vector;

On RDS this requires connecting as the master user. Once the extension is installed
it persists; you only need to do this once per database.
"""

from django.db import migrations, models
from pgvector.django import VectorField


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0012_reference_notes'),
    ]

    operations = [
        migrations.RunSQL(
            'CREATE EXTENSION IF NOT EXISTS vector',
            migrations.RunSQL.noop,
        ),
        migrations.CreateModel(
            name='DocumentChunk',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('source_type', models.CharField(max_length=20)),
                ('source_id', models.CharField(max_length=500)),
                ('source_label', models.CharField(max_length=500)),
                ('chunk_index', models.IntegerField()),
                ('text', models.TextField()),
                ('embedding', VectorField(dimensions=384)),
            ],
            options={'ordering': ['source_id', 'chunk_index']},
        ),
        migrations.RunSQL(
            'CREATE INDEX jobs_documentchunk_embedding_idx '
            'ON jobs_documentchunk USING hnsw (embedding vector_cosine_ops)',
            'DROP INDEX IF EXISTS jobs_documentchunk_embedding_idx',
        ),
    ]
