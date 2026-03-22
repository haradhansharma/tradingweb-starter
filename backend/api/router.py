# backend/api/router.py
from ninja import NinjaAPI

api = NinjaAPI(
    title="SATTAADHAR API",
    version="1.0.0",
    description="Citizen Journalism & Dashboard API",
)


@api.get("/hello")
def hello(request):
    return {"message": "Welcome to Sattaadhar API"}


# We will add more routers here (e.g., api.add_router("/news", news_router))
