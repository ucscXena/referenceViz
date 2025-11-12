from django.urls import path

from . import views

urlpatterns = [
    path('', views.job_list, name='job_list'),
    path('create/', views.JobCreateView.as_view(), name='job_create'),
    path('<uuid:pk>/', views.job_detail, name='job_detail'),
    path('jobs/delete-selected/',
         views.delete_selected_jobs, name='delete_selected_jobs'),
    ]
