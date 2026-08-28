import pytest
from pydantic import ValidationError

from app.config import Settings


def test_deployment_mode_accepts_supported_values(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "production")
    assert Settings(_env_file=None).deployment_mode == "production"


def test_deployment_mode_rejects_typo_instead_of_downgrading_to_development(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "prod")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
