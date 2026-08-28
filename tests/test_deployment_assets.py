from pathlib import Path

from tools.validate_deployment_assets import validate


ROOT = Path(__file__).resolve().parents[1]


def test_deployment_assets_pass_static_preflight():
    assert validate(ROOT) == []


def test_ci_runs_static_deployment_preflight_before_compose_validation():
    workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")

    assert "python tools/validate_deployment_assets.py" in workflow
    assert workflow.index("python tools/validate_deployment_assets.py") < workflow.index("docker compose config --quiet")


def test_deployment_assets_reports_missing_security_contract(tmp_path):
    for name in ("Dockerfile", "docker-compose.yml", ".env.example"):
        source = ROOT / name
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    workflow = tmp_path / ".github/workflows/quality.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text((ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")

    errors = validate(tmp_path)

    assert "container must run as non-root agent" in errors
    assert "container port must be documented" in errors
