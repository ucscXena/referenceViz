from django_rq import job
from .models import Job
import json

from .analysis import perform_analysis

@job('default')  # Use the 'default' queue
def run_analysis(job_id):
    """
    RQ task: Process the uploaded file and update the Job model.
    """
    job_instance = Job.objects.get(id=job_id)
    job_instance.status = 'running'
    job_instance.save()

    try:
        # Run your Python analysis script
        file_path = job_instance.uploaded_file.path
        result_data = perform_analysis(file_path)  # Returns dict/JSON-serializable data

        # Store as JSON (handles dicts or serializes others)
        if isinstance(result_data, dict):
            job_instance.result = result_data
        else:
            job_instance.result = json.loads(json.dumps(result_data))  # Ensure JSON-compatible

        job_instance.status = 'complete'
    except Exception as e:
        job_instance.result = {'error': str(e), 'traceback': str(e.__traceback__)}
        job_instance.status = 'error'
    finally:
        job_instance.save()
