"""Escritura en BigQuery: upsert idempotente por clave, sin columnas hardcodeadas.

El esquema es el de la propia tabla destino (la fuente de la verdad): `upsert` lo
lee con get_table y ejecuta un MERGE pasando las filas como parametro
ARRAY<STRUCT> (UNNEST). Es un QUERY job, NO un load job, asi que no necesita
`bigquery.tables.create` ni tablas temporales: solo `bigquery.jobs.create`
(roles/bigquery.jobUser) + leer/escribir la tabla destino (dlBqTableReader +
dlBqTableWriter). Reprocesar un dataset no duplica filas (idempotente por clave).

Anadir una columna = anadirla a la tabla en BigQuery (y poblar su valor en la
fila desde el job); este fichero no cambia. Las filas se mandan por lotes para no
exceder el tamano maximo de peticion del query job. Los valores TIMESTAMP deben
venir como datetime (no string).
"""
from __future__ import annotations

from typing import Dict, List, Sequence

try:  # el SDK solo hace falta en runtime
    from google.cloud import bigquery
except Exception:  # pragma: no cover - solo si falta el paquete
    bigquery = None  # type: ignore[assignment]

# Tipos legacy del esquema -> tipos de parametro (Standard SQL). El resto se usan
# tal cual (STRING, TIMESTAMP, NUMERIC, JSON, ...).
_PARAM_TYPE = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}


def _param_type(field_type: str) -> str:
    return _PARAM_TYPE.get(field_type.upper(), field_type.upper())


class BigQueryWriter:
    """Inserta/actualiza filas en BigQuery mediante MERGE (idempotente por clave)."""

    # Filas por MERGE: acota el tamano de la peticion del query job.
    BATCH_SIZE = 1000

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

        Columnas y tipos se toman de la tabla destino; cada fila solo necesita las
        claves que quiera escribir (las que falten quedan NULL). Devuelve el numero
        de filas enviadas (0 si no hay).
        """
        if not rows:
            return 0

        schema = self._client.get_table(table_fqn).schema  # fuente de la verdad
        merge_sql = self._merge_sql(table_fqn, schema, key_fields)

        sent = 0
        for start in range(0, len(rows), self.BATCH_SIZE):
            chunk = rows[start:start + self.BATCH_SIZE]
            job_config = bigquery.QueryJobConfig(query_parameters=[self._rows_param(schema, chunk)])
            self._client.query(merge_sql, job_config=job_config).result()
            sent += len(chunk)
        return sent

    @staticmethod
    def _merge_sql(table_fqn: str, schema, key_fields: Sequence[str]) -> str:
        columns = [f.name for f in schema]
        on_clause = " AND ".join(f"T.{k} = S.{k}" for k in key_fields)
        set_cols = [c for c in columns if c not in key_fields]
        matched = ""
        if set_cols:
            set_clause = ", ".join(f"{c} = S.{c}" for c in set_cols)
            matched = f"WHEN MATCHED THEN UPDATE SET {set_clause}\n"
        insert_cols = ", ".join(columns)
        insert_vals = ", ".join(f"S.{c}" for c in columns)
        return (
            f"MERGE `{table_fqn}` T\n"
            f"USING (SELECT * FROM UNNEST(@rows)) S\n"
            f"ON {on_clause}\n"
            f"{matched}"
            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
        )

    @staticmethod
    def _rows_param(schema, rows: List[Dict]) -> "bigquery.ArrayQueryParameter":
        """Construye el parametro @rows como ARRAY<STRUCT<...>> tipado por el esquema."""
        typed = [(f.name, _param_type(f.field_type)) for f in schema]
        struct_type = bigquery.StructQueryParameterType(
            *[bigquery.ScalarQueryParameterType(t, name=n) for n, t in typed]
        )
        structs = [
            bigquery.StructQueryParameter(
                None, *[bigquery.ScalarQueryParameter(n, t, row.get(n)) for n, t in typed]
            )
            for row in rows
        ]
        return bigquery.ArrayQueryParameter("rows", struct_type, structs)
