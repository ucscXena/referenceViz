from django.conf import settings
from django.db import migrations


def populate_uce_model_and_groups(apps, schema_editor):
    UCEModel = apps.get_model('jobs', 'UCEModel')
    ReferenceGroup = apps.get_model('jobs', 'ReferenceGroup')
    Reference = apps.get_model('jobs', 'Reference')
    Job = apps.get_model('jobs', 'Job')

    uce_model = UCEModel.objects.create(
        name=settings.UCE_MODEL_NAME,
        model_url=settings.UCE_MODEL_S3,
        is_default=True,
    )

    for reference in Reference.objects.all():
        group = ReferenceGroup.objects.create(title=reference.name)
        reference.group = group
        reference.uce_model = uce_model
        reference.save()
        group.default_version = reference
        group.save()

    Job.objects.all().update(uce_model=uce_model)


def reverse_populate(apps, schema_editor):
    UCEModel = apps.get_model('jobs', 'UCEModel')
    ReferenceGroup = apps.get_model('jobs', 'ReferenceGroup')
    Reference = apps.get_model('jobs', 'Reference')
    Job = apps.get_model('jobs', 'Job')

    Job.objects.all().update(uce_model=None)
    Reference.objects.all().update(group=None, uce_model=None)
    ReferenceGroup.objects.all().delete()
    UCEModel.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0006_uce_model_reference_group_schema'),
    ]

    operations = [
        migrations.RunPython(populate_uce_model_and_groups, reverse_populate),
    ]
