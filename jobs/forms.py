from django import forms
from .models import Job

class JobUploadForm(forms.ModelForm):
    class Meta:
        model = Job
        fields = ['uploaded_file']   # add more fields later if needed
