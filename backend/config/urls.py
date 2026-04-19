from django.contrib import admin
from django.contrib.auth.views import LoginView
from django.urls import path, include
from django.conf.urls.static import static
from django.conf import settings
from api.router import api  # Import the api instance
from users.views import login_view, dashboard_template_view, profile_view, credentials_view, logout_view
admin.site.site_header = "WebTrading Options Intelligence admin"
admin.site.site_title = "WebTrading Options Intelligence admin"
admin.site.index_title = "WebTrading Options Intelligence administration"

admin.autodiscover()

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    
    # Django template views (session-based auth)
    path("accounts/", dashboard_template_view, name="dashboard"),
    path("accounts/login/", LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("accounts/logout/", logout_view, name="logout"),
    path("accounts/profile/", profile_view, name="profile"),
    path("accounts/credentials/", credentials_view, name="credentials"),
    
    
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)