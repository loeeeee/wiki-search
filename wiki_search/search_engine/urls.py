from django.urls import path
from . import views

urlpatterns = [
    path('', views.search_view, name='search'),
    path('article/<int:page_id>/', views.article_detail_view, name='article_detail'),
    path('status/', views.status_view, name='status'),
]
