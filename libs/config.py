"""Carga el .env / entorno en un objeto tipado (Settings).

Config compartida por TODOS los jobs del Data Lake. Los nombres de variable
coinciden con .env.example. Los FQN de tabla (project.dataset.tabla) se derivan
para no repetirlos por el codigo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:  # python-dotenv es opcional (en Cloud Run las vars vienen del entorno)
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - solo si falta el paquete
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        return False


@dataclass(frozen=True)
class Settings:
    """Configuracion del Data Lake resuelta desde el entorno."""

    # Proyecto / region
    project_id: str
    region: str

    # Vertex / Gemini (privacy, classify)
    vertex_location: str
    gemini_model: str
    anon_method: str

    # Buckets (GCS)
    bkt_raw_public: str
    bkt_public: str
    bkt_raw_user: str
    bkt_user_clean: str
    descriptors_prefix: str

    # BigQuery: ubicacion, datasets y nombres de tabla
    bq_location: str
    ds_raw_metadata: str
    ds_label_review: str
    ds_classification: str
    tbl_images_name: str
    tbl_source_datasets_name: str
    tbl_privacy_name: str
    tbl_label_review_name: str
    tbl_classifications_name: str

    # Parametros de pipeline
    raw_user_ttl_hours: int
    conf_threshold: float

    # Tablas (FQN project.dataset.tabla)
    @property
    def tbl_images(self) -> str:
        return f"{self.project_id}.{self.ds_raw_metadata}.{self.tbl_images_name}"

    @property
    def tbl_source_datasets(self) -> str:
        return f"{self.project_id}.{self.ds_raw_metadata}.{self.tbl_source_datasets_name}"

    @property
    def tbl_user_images_privacy(self) -> str:
        return f"{self.project_id}.{self.ds_raw_metadata}.{self.tbl_privacy_name}"

    @property
    def tbl_label_review_status(self) -> str:
        return f"{self.project_id}.{self.ds_label_review}.{self.tbl_label_review_name}"

    @property
    def tbl_classifications(self) -> str:
        return f"{self.project_id}.{self.ds_classification}.{self.tbl_classifications_name}"

    # URIs base
    def raw_public_uri(self, dataset: str) -> str:
        return f"gs://{self.bkt_raw_public}/{dataset}"

    def public_uri(self, dataset: str) -> str:
        return f"gs://{self.bkt_public}/{dataset}"

    def descriptor_uri(self, dataset: str) -> str:
        prefix = self.descriptors_prefix.strip("/")
        return f"gs://{self.bkt_raw_public}/{prefix}/{dataset}.yml"


def _env(name: str) -> str:
    value = os.environ.get(name)
    if value in (None, ""):
        raise RuntimeError(f"Falta la variable de entorno requerida: {name}")
    return value


def load_settings(env_file: str | None = None) -> Settings:
    """Lee el .env (si existe) y el entorno, y devuelve un Settings tipado."""
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()
    return Settings(
        project_id=_env("GCP_PROJECT_ID"),
        region=_env("GCP_REGION"),
        vertex_location=_env("VERTEX_LOCATION"),
        gemini_model=_env("GEMINI_MODEL"),
        anon_method=_env("ANON_METHOD"),
        bkt_raw_public=_env("BKT_RAW_PUBLIC"),
        bkt_public=_env("BKT_PUBLIC"),
        bkt_raw_user=_env("BKT_RAW_USER"),
        bkt_user_clean=_env("BKT_USER_CLEAN"),
        descriptors_prefix=_env("DESCRIPTORS_PREFIX"),
        bq_location=_env("BQ_LOCATION"),
        ds_raw_metadata=_env("BQ_DS_RAW_METADATA"),
        ds_label_review=_env("BQ_DS_LABEL_REVIEW"),
        ds_classification=_env("BQ_DS_CLASSIFICATION"),
        tbl_images_name=_env("BQ_TBL_IMAGES"),
        tbl_source_datasets_name=_env("BQ_TBL_SOURCE_DATASETS"),
        tbl_privacy_name=_env("BQ_TBL_PRIVACY"),
        tbl_label_review_name=_env("BQ_TBL_LABEL_REVIEW"),
        tbl_classifications_name=_env("BQ_TBL_CLASSIFICATIONS"),
        raw_user_ttl_hours=int(_env("RAW_USER_TTL_HOURS")),
        conf_threshold=float(_env("CONF_THRESHOLD")),
    )
