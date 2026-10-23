from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0020_alter_userprofile_email_on_complete_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='ucemodel',
            name='pipeline',
            field=models.CharField(
                choices=[('uce', 'UCE'), ('uce-brain', 'UCE Brain')],
                default='uce',
                max_length=20,
            ),
        ),
    ]
