from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    # Add giant-project essentials now so they are in the DB
    is_verified = models.BooleanField(default=False)
    bio = models.TextField(max_length=500, blank=True)
    reputation = models.IntegerField(default=0)
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)

    def __str__(self):

        return self.username
