import django.db.models.deletion

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0007_uce_model_reference_group_data'),
    ]

    operations = [
        migrations.AlterField(
            model_name='reference',
            name='group',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='versions',
                to='jobs.referencegroup',
            ),
        ),
        migrations.AlterField(
            model_name='reference',
            name='uce_model',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='references',
                to='jobs.ucemodel',
            ),
        ),
        migrations.RemoveField(
            model_name='reference',
            name='name',
        ),
        migrations.AlterModelOptions(
            name='reference',
            options={'ordering': ['group__title']},
        ),
        migrations.AlterModelOptions(
            name='projection',
            options={'ordering': ['reference__group__title']},
        ),
    ]
