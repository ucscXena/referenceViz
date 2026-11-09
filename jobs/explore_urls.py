from django.urls import path

from . import views

urlpatterns = [
    path('', views.reference_explore, name='reference_explore'),
    path('<uuid:reference_id>/', views.reference_detail, name='reference_detail'),
    path('<uuid:reference_id>/cells/', views.reference_detail, {'tab': 'cells'}, name='reference_cells'),
    path('<uuid:reference_id>/publications/', views.reference_detail, {'tab': 'publications'},
         name='reference_publications'),
    path('<uuid:reference_id>/wrangling-notes/', views.reference_detail, {'tab': 'wrangling_notes'},
         name='reference_wrangling_notes'),
]
