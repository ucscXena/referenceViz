from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0017_job_current_conversation_conversationmessage'),
    ]

    operations = [
        migrations.AlterField(
            model_name='job',
            name='status',
            field=models.CharField(
                choices=[
                    ('uploading', 'Uploading'),
                    ('pending', 'Pending'),
                    ('running', 'Running'),
                    ('complete', 'Complete'),
                    ('error', 'Error'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]
