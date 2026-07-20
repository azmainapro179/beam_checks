from django.urls import path
from .views import upload_dxf, get_structure_types
from checker import views

urlpatterns = [
    path('', upload_dxf),
    path(
        'get-structure-types/',
        views.get_structure_types,
        name='get_structure_types'
    ),
]