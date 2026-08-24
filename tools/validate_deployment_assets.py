"""Validate deployment files without requiring Docker to be installed.

This is a static preflight check. It intentionally does not claim that an image
build, Compose interpolation, or vulnerability scan has run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def validate(root: Path) -> list[str]:
    errors: list[str] = []

    def read(name: str) -> str:
        path = root / name
        if not path.is_file():
            errors.append(f"missing file: {name}")
            return ""
        return path.read_text(encoding="utf-8")

    dockerfile = read("Dockerfile")
    compose = read("docker-compose.yml")
    workflow = read(".github/workflows/quality.yml")
    env_example = read(".env.example")

    required_dockerfile = {
        "USER agent": "container must run as non-root agent",
        "EXPOSE 8030": "container port must be documented",
        'CMD ["uvicorn"': "container must start the FastAPI service",
    }
    for marker, message in required_dockerfile.items():
        if marker not in dockerfile:
            errors.append(message)

    required_compose = {
        "healthcheck:": "Compose healthcheck is missing",
        "agent-runtime:/app/runtime": "platform runtime volume is missing",
        ":ro": "read-only business data mount is missing",
        "no-new-privileges:true": "container privilege hardening is missing",
        "127.0.0.1:8030/ready": "healthcheck must use the readiness endpoint",
    }
    for marker, message in required_compose.items():
        if marker not in compose:
            errors.append(message)

    required_ci = {
        "docker compose config --quiet": "CI must validate Compose interpolation",
        "docker build": "CI must build the image",
        "trivy-action": "CI must scan the image",
        "pip_audit": "CI must audit Python dependencies",
    }
    for marker, message in required_ci.items():
        if marker not in workflow:
            errors.append(message)

    for variable in ("ADMIN_API_KEY", "ENERGY_DB_PATH", "ENERGY_DDL_PATH"):
        if variable not in env_example:
            errors.append(f".env.example missing {variable}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = validate(args.root.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Deployment assets passed static preflight.")
    print("Note: Docker build, Compose startup, and Trivy scan still require a CI or Docker environment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
