from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0024_publication'),
    ]

    operations = [
        migrations.AddField(
            model_name='publication',
            name='fulltext',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='publication',
            name='fulltext_source',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name='publication',
            name='fulltext_status',
            field=models.CharField(
                choices=[('none', 'Not fetched'), ('fetching', 'Fetching'),
                         ('complete', 'Complete'), ('error', 'Error')],
                default='none', max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='publication',
            name='fulltext_error',
            field=models.TextField(blank=True),
        ),
    ]
