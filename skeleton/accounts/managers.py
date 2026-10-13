from django.contrib.auth.base_user import BaseUserManager
from django.db import transaction

from skeleton.common.querysets import RecordManager


class UserManager(RecordManager, BaseUserManager):
    use_in_migrations = True

    @classmethod
    def normalize_email(cls, email):
        return super().normalize_email(email).strip().lower() if email else ""

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        from skeleton.accounts.services import ensure_profile
        with transaction.atomic(using=self.db):
            user.save(using=self.db)
            ensure_profile(user, using=self.db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        if not all(extra_fields.get(flag) is True for flag in ("is_staff", "is_superuser", "is_active")):
            raise ValueError("A superuser must be active, staff and superuser.")
        return self.create_user(email, password, **extra_fields)
