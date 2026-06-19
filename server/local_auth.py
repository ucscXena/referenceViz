import os

from django.contrib.auth import get_user_model


class ForceUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        email = os.environ.get("FORCE_USER_EMAIL")
        if email:
            User = get_user_model()
            try:
                request.user = User.objects.get(email=email)
            except User.DoesNotExist:
                pass
        return self.get_response(request)
