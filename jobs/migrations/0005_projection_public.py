from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0004_reference_projection'),
    ]

    operations = [
        migrations.AddField(
            model_name='projection',
            name='public',
            field=models.BooleanField(default=False),
        ),
    ]
