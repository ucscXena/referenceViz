from django.shortcuts import render

from django.views.generic.edit import CreateView
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST
from .models import Job
from .tasks import run_analysis
from .forms import JobUploadForm

class JobCreateView(CreateView):
    model = Job
    form_class = JobUploadForm
    template_name = 'jobs/create.html'
    success_url = reverse_lazy('job_list')

    def form_valid(self, form):
        job = form.save(commit=False)
        job.user = self.request.user
        job.save()
        # XXX isn't UUID already a str?
        rq_job = run_analysis.enqueue(str(job.id))
        return super().form_valid(form)

@login_required
def job_list(request):
    """
    Render a page that shows every Job belonging to the current user,
    ordered newest-first.
    """
    jobs = Job.objects.filter(user=request.user).order_by('-created_at')
    return render(
        request,
        'jobs/list.html',
        {'jobs': jobs}
    )

@login_required
def job_detail(request, pk):
    """
    Show full job details.
    If status == 'error', display the error message from job.result.
    """
    job = get_object_or_404(Job, pk=pk, user=request.user)
    return render(request, 'jobs/detail.html', {'job': job})

@require_POST
@login_required
def delete_selected_jobs(request):
    """
    Delete jobs that belong to the user and are checked in the form.
    """
    selected_ids = request.POST.getlist('job_ids')  # list of UUID strings
    # Filter by user + selected IDs
    Job.objects.filter(user=request.user, id__in=selected_ids).delete()
    return redirect('job_list')
