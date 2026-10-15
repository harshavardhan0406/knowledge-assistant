from django.urls import path

from . import views

urlpatterns = [
    path("", views.index),
    path("api/health/", views.health),
    path("api/documents/", views.documents),
    path("api/documents/<int:pk>/", views.document_detail),
    path("api/ask/", views.ask),
    path("api/summarize/", views.summarize_view),
]