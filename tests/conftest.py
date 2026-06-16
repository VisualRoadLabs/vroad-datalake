"""Configuracion compartida de tests para todo el repo.

Estructura:
    tests/
      conftest.py            <- este archivo: raiz del repo en path (`libs`) + fixture `cloud`
      libs/                  <- tests de libs/
      jobs/<job>/            <- tests por job (cada conftest del job anade su propio `src`)

- Inyecta el trust store del sistema operativo (truststore) para que TLS contra
  cloud funcione detras de un proxy corporativo que inspecciona SSL.
- Fixture `cloud`: proyecto real + cliente de Storage, SOLO LECTURA. Si cloud
  no esta accesible (sin `gcloud auth application-default login`, sin red), los
  tests que lo usan se saltan con un mensaje accionable. Ningun test escribe en cloud.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)  # -> `import libs.*`


def _load_deploy_env() -> None:
    """Carga deploy/env.yaml en el entorno para que load_settings() funcione en CI.

    Cloud Build no inyecta el .env; las variables viven en deploy/env.yaml (la
    misma fuente que usa el deploy del job). No pisa variables ya definidas, asi
    que un .env local o el entorno real tienen prioridad.
    """
    path = os.path.join(_REPO_ROOT, "deploy", "env.yaml")
    if not os.path.exists(path):
        return
    try:
        import yaml
    except Exception:  # noqa: BLE001 - sin PyYAML no se puede cargar; se deja al .env
        return
    with open(path, encoding="utf-8") as fh:
        for key, value in (yaml.safe_load(fh) or {}).items():
            os.environ.setdefault(key, str(value))


_load_deploy_env()


def _use_os_truststore() -> None:
    """Hace que Python use el trust store del SO (donde vive la CA corporativa)."""
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001 - usa el bundle de CA por defecto
        pass


@pytest.fixture(scope="session")
def cloud():
    """Acceso real a cloud, solo lectura. Salta si no hay credenciales/red."""
    _use_os_truststore()

    from libs.config import load_settings

    try:
        settings = load_settings()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Config no disponible ({e}). Define deploy/env.yaml o un .env.")

    try:
        import google.auth

        _creds, detected = google.auth.default()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"No ADC credentials ({e}). Run: gcloud auth application-default login")
    project = detected or settings.project_id

    from google.cloud import storage

    client = storage.Client(project=project)
    try:
        # Comprobacion barata de solo lectura: 1 objeto, timeout corto.
        next(iter(client.list_blobs(settings.bkt_raw_public, max_results=1, timeout=15)), None)
    except Exception as e:  # noqa: BLE001
        pytest.skip(
            f"Cloud not reachable (read-only) ({type(e).__name__}: {str(e)[:140]}). "
            "Check network and run: gcloud auth application-default login"
        )

    return {"project": project, "settings": settings, "storage": client}
