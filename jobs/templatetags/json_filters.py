# myapp/templatetags/json_filters.py
import json
from django import template
from django.utils.safestring import mark_safe

register = template.Library()

@register.filter
def tojson(value):
    if value is None:
        return "null"
    return mark_safe(json.dumps(value, indent=2))
