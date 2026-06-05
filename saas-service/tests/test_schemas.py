"""Tests for Pydantic schemas."""

import pytest
from pydantic import ValidationError

from app.schemas import (
    UserCreate, UserUpdate, UserOut,
    TenantCreate, TenantUpdate, TenantOut,
    ExperimentCreate, ExperimentUpdate, ExperimentOut,
)


def test_user_create_valid():
    user = UserCreate(email="test@example.com", password="secret123", full_name="Test")
    assert user.email == "test@example.com"
    assert user.password == "secret123"
    assert user.role == "viewer"


def test_user_create_invalid_email():
    with pytest.raises(ValidationError):
        UserCreate(email="not-an-email", password="secret123")


def test_user_update_partial():
    update = UserUpdate(full_name="New Name")
    dumped = update.model_dump(exclude_unset=True)
    assert "full_name" in dumped
    assert "email" not in dumped


def test_tenant_create():
    tenant = TenantCreate(name="Acme Corp", slug="acme")
    assert tenant.name == "Acme Corp"
    assert tenant.slug == "acme"


def test_tenant_update_partial():
    update = TenantUpdate(name="New Name")
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"name": "New Name"}
