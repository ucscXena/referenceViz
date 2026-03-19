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

# Base URL of this server for internal callbacks (e.g. 'http://10.0.0.1')
SERVER_BASE_URL = ''

# AWS / Batch — override these in site_settings_private.py
AWS_REGION = 'us-east-1'
AWS_S3_BUCKET = ''
# UCE embedding pipeline (Batch)
UCE_BATCH_JOB_QUEUE = ''
UCE_BATCH_JOB_DEFINITION = ''
UCE_MODEL_S3 = ''
# Cell-type projection pipeline (Batch)
BATCH_JOB_QUEUE = ''
BATCH_JOB_DEFINITION = ''
# Optional explicit credentials (prefer IAM role or ~/.aws/credentials instead)
AWS_ACCESS_KEY_ID = ''
AWS_SECRET_ACCESS_KEY = ''

# Time estimates for the job list
# UCE embedding (g5.12xlarge, 4 GPUs)
UCE_STARTUP_SECONDS = 351        # fixed overhead before cell processing begins
UCE_SECONDS_PER_CELL_PER_GPU = 526 / 10000 / 4   # per cell, normalised to 1 GPU
# Cell-type projection (r7i.4xlarge)
PROJ_STARTUP_SECONDS = 216       # fixed overhead
PROJ_SECONDS_PER_CELL = 169 / 10000  # per cell

# uncomment to test allauth
#from .site_settings_private import *
