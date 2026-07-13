"""Single runtime source for the HealthClaw release version."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


try:
    __version__ = version("healthclaw-guardrails")
except PackageNotFoundError:  # source tree without an installed distribution
    metadata = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    __version__ = str(metadata["project"]["version"])
