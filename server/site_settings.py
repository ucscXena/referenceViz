import os

ALLOWED_HOSTS = []

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'development'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Database
# https://docs.djangoproject.com/en/1.10/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
    }
}

# AWS / SageMaker — override these in site_settings_private.py
AWS_REGION = 'us-east-1'
AWS_S3_BUCKET = ''
SAGEMAKER_ENDPOINT_NAME = ''
# Optional explicit credentials (prefer IAM role or ~/.aws/credentials instead)
AWS_ACCESS_KEY_ID = ''
AWS_SECRET_ACCESS_KEY = ''

# uncomment to test allauth
#from .site_settings_private import *
