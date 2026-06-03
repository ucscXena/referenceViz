from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0016_alter_sharetoken_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='job',
            name='current_conversation',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.CreateModel(
            name='ConversationMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('generation', models.PositiveSmallIntegerField(default=0)),
                ('role', models.CharField(max_length=10)),
                ('content', models.TextField()),
                ('charts', models.JSONField(default=list)),
                ('suggestions', models.JSONField(default=list)),
                ('hidden', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='conversation_messages', to='jobs.job')),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
    ]
