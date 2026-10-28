"""
Extension registry: optional modules a deployment plugs into the core.

The core (the guardrail layer: tenancy, step-up, redaction, audit, the FHIR
surface) never imports an optional module directly. An optional module is
reached only through this registry, so the core starts, serves and passes
its own suite whether or not any extension is installed.

An extension is a callable ``register(registry)``. It declares what it adds
by calling the ``add_*`` methods below; the core then applies each
contribution at the point where it belongs:

- ``add_r6_routes(fn, operations=...)`` — ``fn(blueprint, helpers)`` adds
  routes to the FHIR blueprint. Called by ``r6/routes.py`` at import time,
  before the blueprint is registered on an app. ``operations`` names the
  FHIR operations the routes serve; an operation the core knows about and
  no extension claims answers ``501 not-supported`` (``OPTIONAL_OPERATIONS``).
- ``add_app_hook(fn)`` — ``fn(app)`` registers blueprints, CLI commands and
  the like on each app ``create_app`` builds.
- ``add_model_module(name)`` — a module whose import declares SQLAlchemy
  tables, so ``create_all`` and the migration helpers see them.
- ``add_purge_model(import_path, attr, label)`` — a tenant-owned table the
  tenant purge must empty. Order is kept: a table whose rows reference
  another's is listed first.

Discovery, read once per process:

- ``HEALTHCLAW_EXTENSIONS`` set: exactly the comma-separated
  ``module:callable`` (or ``module``, meaning ``module:register``) entries
  it names, in order. Set to the empty string, no extension loads.
- unset: every entry point in the ``healthclaw.extensions`` group, then the
  modules bundled with this repository (``BUNDLED``) if they are present.

A named or installed extension that fails to import fails startup. A
bundled default that is absent is skipped: it is optional by definition.
"""

from __future__ import annotations

import importlib
import logging
import os
from importlib import metadata
from typing import Callable

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "healthclaw.extensions"
ENV_VAR = "HEALTHCLAW_EXTENSIONS"

#: Loaded when ``HEALTHCLAW_EXTENSIONS`` is unset and the module is present.
BUNDLED = ("bundled_extensions:register",)

#: FHIR operations the core routes to an extension. Path relative to the
#: FHIR base, and the methods the route answers.
OPTIONAL_OPERATIONS = {
    "Observation/$interpret": ("POST",),
    "Patient/$care-gaps": ("GET", "POST"),
}


class ExtensionRegistry:
    """What the loaded extensions contribute, in registration order."""

    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.r6_routes: list[Callable] = []
        self.app_hooks: list[Callable] = []
        self.model_modules: list[str] = []
        self.purge_models: list[tuple[str, str, str]] = []
        self.operations: set[str] = set()

    def add_r6_routes(self, fn: Callable, operations=()) -> None:
        self.r6_routes.append(fn)
        self.operations.update(operations)

    def add_app_hook(self, fn: Callable) -> None:
        self.app_hooks.append(fn)

    def add_model_module(self, name: str) -> None:
        self.model_modules.append(name)

    def add_purge_model(self, import_path: str, attr: str, label: str) -> None:
        self.purge_models.append((import_path, attr, label))

    def provides(self, operation: str) -> bool:
        return operation in self.operations


def _resolve(spec: str) -> Callable:
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name.strip())
    return getattr(module, (attr or "register").strip())


def _discover() -> list[tuple[str, Callable]]:
    raw = os.environ.get(ENV_VAR)
    if raw is not None:
        return [(spec.strip(), _resolve(spec))
                for spec in raw.split(",") if spec.strip()]

    found = [(ep.value, ep.load())
             for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)]
    for spec in BUNDLED:
        if spec in (name for name, _ in found):
            continue
        try:
            found.append((spec, _resolve(spec)))
        except ModuleNotFoundError as exc:
            if exc.name != spec.partition(":")[0]:
                raise  # present but broken: fail loudly
            logger.info("extensions: %s not present", spec)
    return found


_registry: ExtensionRegistry | None = None


def get_registry() -> ExtensionRegistry:
    """The process-wide registry, built on first use."""
    global _registry
    if _registry is None:
        registry = ExtensionRegistry()
        for name, register in _discover():
            register(registry)
            registry.loaded.append(name)
        logger.info("extensions loaded: %s",
                    ", ".join(registry.loaded) or "none")
        _registry = registry
    return _registry


def import_model_modules() -> None:
    """Import every extension's model modules (see ``add_model_module``)."""
    for name in get_registry().model_modules:
        importlib.import_module(name)
