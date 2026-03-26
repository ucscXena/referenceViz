from django.urls import path

from . import views

urlpatterns = [
    path('', views.job_list, name='job_list'),
    path('references/', views.reference_list, name='reference_list'),
    path('create/', views.upload_page, name='job_create'),
    path('upload-url/', views.get_upload_url, name='upload_url'),
    path('presign/', views.presign_overlay, name='presign_overlay'),
    path('<uuid:pk>/', views.job_detail, name='job_detail'),
    path('<uuid:pk>/status/', views.job_status, name='job_status'),
    path('<uuid:pk>/download/', views.download_result, name='download_result'),
    path('<uuid:job_id>/abort/', views.abort_upload, name='abort_upload'),
    path('<uuid:job_id>/confirm/', views.confirm_upload, name='confirm_upload'),
    path('<uuid:job_id>/project/', views.project_existing, name='project_existing'),
    path('projections/<uuid:pk>/download/', views.download_projection, name='download_projection'),
    path('projections/<uuid:pk>/set-public/', views.set_projection_public, name='set_projection_public'),
    path('jobs/delete-selected/', views.delete_selected_jobs, name='delete_selected_jobs'),
    path('uce-callback/', views.uce_callback, name='uce_callback'),
    path('projection-callback/', views.projection_callback, name='projection_callback'),
    path('user-status/', views.user_status, name='user_status'),
]
