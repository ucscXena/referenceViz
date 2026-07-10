from django.conf import settings


def ga_id(request):
    return {'GA_ID': getattr(settings, 'GA_ID', '')}
