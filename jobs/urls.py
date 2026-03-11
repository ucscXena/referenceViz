from django.urls import path

from . import views

urlpatterns = [
    path('', views.job_list, name='job_list'),
    path('references/', views.reference_list, name='reference_list'),
    path('create/', views.upload_page, name='job_create'),
    path('upload-url/', views.get_upload_url, name='upload_url'),
    path('<uuid:pk>/', views.job_detail, name='job_detail'),
    path('<uuid:pk>/status/', views.job_status, name='job_status'),
    path('<uuid:pk>/download/', views.download_result, name='download_result'),
    path('<uuid:job_id>/confirm/', views.confirm_upload, name='confirm_upload'),
    path('<uuid:job_id>/project/', views.project_existing, name='project_existing'),
    path('projections/<uuid:pk>/download/', views.download_projection, name='download_projection'),
    path('jobs/delete-selected/', views.delete_selected_jobs, name='delete_selected_jobs'),
]
