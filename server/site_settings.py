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
AWS_BATCH_CONSOLE_URL = 'http://placeholder/detail/{}'
AWS_S3_BUCKET = ''
EXAMPLE_FILE_S3_KEY = ''   # e.g. 'example/example.h5ad'; leave empty to hide the example button
# UCE embedding pipeline (Batch)
UCE_BATCH_JOB_QUEUE = ''
UCE_BATCH_JOB_DEFINITION = ''
UCE_MODEL_NAME = ''   # display name for the initial UCEModel row, e.g. 'UCE 1.0'
UCE_MODEL_S3 = ''     # model_url stored on UCEModel; passed as model_s3 to Batch
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

# Chatbot
ANTHROPIC_API_KEY = ''
# Where sentence-transformers caches downloaded models.
# Production: deploy script downloads here as ubuntu, then chmod a+rX so www-data can read.
# Locally: override in site_settings_private.py if your st-models dir is elsewhere.
SENTENCE_TRANSFORMERS_HOME = os.path.join(os.path.dirname(BASE_DIR), 'st-models')
# Absolute path to UCSC_Brain_Explorer_metadata.json
# Deployed alongside the Django project by the deploy script; override locally in site_settings_private.py
BRAIN_EXPLORER_METADATA_PATH = os.path.join(BASE_DIR, 'UCSC_Brain_Explorer_metadata.json')
# Directory where fetched paper text files are stored; override in site_settings_private.py
PAPERS_DIR = os.path.join(BASE_DIR, 'papers')

# uncomment to test allauth
from .site_settings_private import *
