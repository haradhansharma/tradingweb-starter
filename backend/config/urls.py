from django.contrib import admin
from django.urls import path, include
from django.conf.urls.static import static
from django.conf import settings
from api.router import api  # Import the api instance

admin.site.site_header = "WebTrading Options Intelligence admin"
admin.site.site_title = "WebTrading Options Intelligence admin"
admin.site.index_title = "WebTrading Options Intelligence administration"

admin.autodiscover()

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    path("i18n/", include("django.conf.urls.i18n")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)