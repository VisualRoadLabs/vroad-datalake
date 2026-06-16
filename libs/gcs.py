"""Cliente fino sobre Google Cloud Storage.

Trabaja siempre con URIs gs://bucket/objeto. Es el "leer/escribir objetos"
comun a todos los jobs. Los adaptadores dependen solo de un subconjunto
(read_text / read_bytes / exists / list).
"""
from __future__ import annotations

from typing import List, Tuple

try:  # el SDK solo hace falta en runtime, no en los tests de adaptadores
    from google.cloud import storage
except Exception:  # pragma: no cover - solo si falta el paquete
    storage = None  # type: ignore[assignment]

try:  # politica de reintentos para errores transitorios (503/429/5xx, conexion)
    from google.cloud.storage.retry import DEFAULT_RETRY
except Exception:  # pragma: no cover
    DEFAULT_RETRY = None

if DEFAULT_RETRY is not None:
    try:
        _RETRY = DEFAULT_RETRY.with_timeout(300.0)
    except AttributeError:  # pragma: no cover - api-core antiguo
        _RETRY = DEFAULT_RETRY.with_deadline(300.0)
else:  # pragma: no cover
    _RETRY = None

# Se fuerza el reintento (no condicional) tambien en copy/upload: el contenido es
# determinista, asi que repetir el POST/PUT tras un 503 es seguro e idempotente.
_RETRY_KW = {"retry": _RETRY} if _RETRY is not None else {}


def parse_uri(uri: str) -> Tuple[str, str]:
    """gs://bucket/a/b -> ('bucket', 'a/b')."""
    if not uri.startswith("gs://"):
        raise ValueError(f"URI GCS invalida (falta gs://): {uri!r}")
    bucket, _, obj = uri[len("gs://"):].partition("/")
    if not bucket:
        raise ValueError(f"URI GCS sin bucket: {uri!r}")
    return bucket, obj


class GcsClient:
    """Wrapper de google.cloud.storage con la API que usan los jobs."""

    def __init__(self, client: "storage.Client | None" = None):
        if client is None:
            if storage is None:
                raise RuntimeError(
                    "google-cloud-storage no esta instalado; "
                    "instala las dependencias o inyecta un client de test."
                )
            client = storage.Client()
        self._client = client

    # lectura
    def read_bytes(self, uri: str) -> bytes:
        bucket, obj = parse_uri(uri)
        return self._client.bucket(bucket).blob(obj).download_as_bytes(**_RETRY_KW)

    def read_text(self, uri: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(uri).decode(encoding)

    def exists(self, uri: str) -> bool:
        bucket, obj = parse_uri(uri)
        return self._client.bucket(bucket).blob(obj).exists(**_RETRY_KW)

    def list(self, prefix_uri: str) -> List[str]:
        """Lista recursiva de objetos bajo el prefijo, como URIs gs://."""
        bucket, prefix = parse_uri(prefix_uri)
        return [
            f"gs://{bucket}/{blob.name}"
            for blob in self._client.list_blobs(bucket, prefix=prefix, **_RETRY_KW)
        ]

    # escritura
    def upload_bytes(self, uri: str, data: bytes, content_type: str | None = None) -> None:
        bucket, obj = parse_uri(uri)
        self._client.bucket(bucket).blob(obj).upload_from_string(
            data, content_type=content_type, **_RETRY_KW
        )

    def copy(self, src_uri: str, dst_uri: str) -> None:
        """Copia objeto a objeto (sirve entre buckets)."""
        src_bucket, src_obj = parse_uri(src_uri)
        dst_bucket, dst_obj = parse_uri(dst_uri)
        source_bucket = self._client.bucket(src_bucket)
        source_blob = source_bucket.blob(src_obj)
        source_bucket.copy_blob(
            source_blob, self._client.bucket(dst_bucket), dst_obj, **_RETRY_KW
        )
