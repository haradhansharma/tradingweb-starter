# backend/config/urls.py

from django.contrib import admin
from django.urls import path, include
from django.conf.urls.static import static
from django.conf import settings
from api.router import api  # Import the api instance

admin.site.site_header = "SATTAADHAR admin"
admin.site.site_title = "SATTAADHAR admin"
# admin.site.site_url = ''
admin.site.index_title = "SATTAADHAR administration"
# admin.empty_value_display = '**Empty**'

admin.autodiscover()
# admin.site.login = secure_admin_login(admin.site.login)  # type: ignore

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    path("i18n/", include("django.conf.urls.i18n")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
