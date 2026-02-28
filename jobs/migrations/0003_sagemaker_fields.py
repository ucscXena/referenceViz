from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0002_auto_20251107_0508'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='job',
            name='uploaded_file',
        ),
        migrations.AddField(
            model_name='job',
            name='s3_input_key',
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='job',
            name='s3_output_key',
            field=models.CharField(blank=True, max_length=500),
        ),
    ]
