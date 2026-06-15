"""Escritura en BigQuery: upsert idempotente por clave, sin columnas hardcodeadas.

El esquema es el de la propia tabla destino (la fuente de la verdad): `upsert`
lo lee con get_table, carga las filas en una tabla temporal con ese esquema y
hace MERGE por la(s) clave(s) -> reprocesar un dataset no duplica filas.
Anadir una columna = anadirla a la tabla en BigQuery (y poblar su valor en la
fila desde el job); este fichero no cambia.

NOTA IAM: el MERGE y el load job requieren `roles/bigquery.jobUser` a nivel de
proyecto para la SA que ejecuta el job.
"""
from __future__ import annotations

import uuid
from typing import Dict, List, Sequence

try:  # el SDK solo hace falta en runtime
    from google.cloud import bigquery
except Exception:  # pragma: no cover - solo si falta el paquete
    bigquery = None  # type: ignore[assignment]


class BigQueryWriter:
    """Inserta/actualiza filas en BigQuery mediante MERGE (idempotente por clave)."""

    def __init__(self, client: "bigquery.Client | None" = None, location: str | None = None):
        if client is None:
            if bigquery is None:
                raise RuntimeError(
                    "google-cloud-bigquery no esta instalado; "
                    "instala las dependencias o inyecta un client de test."
                )
            client = bigquery.Client(location=location)
        self._client = client
        self._location = location

    def upsert(self, table_fqn: str, rows: List[Dict], key_fields: Sequence[str]) -> int:
        """Upsert idempotente de `rows` en `table_fqn` por `key_fields`.

        Las columnas y tipos se toman de la tabla destino; cada fila solo necesita
        traer las claves que quiera escribir (las que falten quedan NULL). Devuelve
        el numero de filas enviadas (0 si no hay).
        """
        if not rows:
            return 0

        table = self._client.get_table(table_fqn)  # esquema = el de la tabla destino
        columns = [field.name for field in table.schema]

        tmp_fqn = f"{table_fqn}__stg_{uuid.uuid4().hex[:8]}"
        load_job = self._client.load_table_from_json(
            rows,
            tmp_fqn,
            job_config=bigquery.LoadJobConfig(
                schema=table.schema,
                write_disposition="WRITE_TRUNCATE",
            ),
        )
        load_job.result()
        try:
            on_clause = " AND ".join(f"T.{k} = S.{k}" for k in key_fields)
            set_cols = [c for c in columns if c not in key_fields]
            matched = ""
            if set_cols:
                set_clause = ", ".join(f"{c} = S.{c}" for c in set_cols)
                matched = f"WHEN MATCHED THEN UPDATE SET {set_clause}\n"
            insert_cols = ", ".join(columns)
            insert_vals = ", ".join(f"S.{c}" for c in columns)
            merge_sql = (
                f"MERGE `{table_fqn}` T\n"
                f"USING `{tmp_fqn}` S\n"
                f"ON {on_clause}\n"
                f"{matched}"
                f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
            )
            self._client.query(merge_sql).result()
        finally:
            self._client.delete_table(tmp_fqn, not_found_ok=True)
        return len(rows)
