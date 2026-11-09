"""
Move Crossref details out of Reference.metadata into Publication rows, so that
the metadata holds only what the wranglers wrote.
"""
from django.db import migrations, models

CROSSREF_FIELDS = ('title', 'authors', 'journal', 'year')


def move_to_publications(apps, schema_editor):
    Publication = apps.get_model('jobs', 'Publication')
    Reference = apps.get_model('jobs', 'Reference')
    for reference in Reference.objects.exclude(metadata=None):
        metadata = reference.metadata
        for pub in metadata.get('publication') or []:
            doi = pub.get('doi')
            if not doi:
                continue
            found = {f: str(pub.pop(f)) for f in CROSSREF_FIELDS if f in pub}
            if found.get('title'):
                Publication.objects.get_or_create(doi=doi, defaults=found)
        for doi in metadata.pop('_crossref_failed', None) or []:
            Publication.objects.get_or_create(doi=doi, defaults={'fetch_error': 'Crossref fetch failed'})
        Reference.objects.filter(pk=reference.pk).update(metadata=metadata)


def move_to_metadata(apps, schema_editor):
    Publication = apps.get_model('jobs', 'Publication')
    Reference = apps.get_model('jobs', 'Reference')
    found = {p.doi: p for p in Publication.objects.all()}
    for reference in Reference.objects.exclude(metadata=None):
        metadata = reference.metadata
        failed = []
        for pub in metadata.get('publication') or []:
            row = found.get(pub.get('doi'))
            if row and row.title:
                pub.update({f: getattr(row, f) for f in CROSSREF_FIELDS})
            elif row and row.fetch_error:
                failed.append(row.doi)
        if failed:
            metadata['_crossref_failed'] = failed
        Reference.objects.filter(pk=reference.pk).update(metadata=metadata)


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0023_reference_metadata'),
    ]

    operations = [
        migrations.CreateModel(
            name='Publication',
            fields=[
                ('doi', models.CharField(max_length=255, primary_key=True, serialize=False)),
                ('title', models.TextField(blank=True)),
                ('authors', models.TextField(blank=True)),
                ('journal', models.TextField(blank=True)),
                ('year', models.CharField(blank=True, max_length=10)),
                ('fetch_error', models.TextField(blank=True)),
                ('fetched_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'ordering': ['doi'],
            },
        ),
        migrations.RunPython(move_to_publications, move_to_metadata),
        migrations.RemoveField(
            model_name='reference',
            name='metadata_generation',
        ),
    ]
