from allauth.account.adapter import DefaultAccountAdapter
from django.http import HttpResponseNotAllowed

class NoSignupAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False
