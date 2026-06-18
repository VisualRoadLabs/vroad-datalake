# BigQuery Reference (datasets & tables)

Datasets y tablas de BigQuery del Data Lake de Visual Road, quién las lee/escribe y
el diseño de escritura. Región **`us-central1`**. Modo: **R** = REQUIRED, **N** =
NULLABLE.

## Datasets (4)

| Dataset | Contenido | Escribe | Lee |
|---|---|---|---|
| `ds_raw_metadata` | Catálogo de imágenes, datasets públicos, auditoría de anonimización | ingest-public, privacy | classify |
| `ds_label_review` | Cola de revisión de etiquetas (baja confianza) | privacy *(futuro)* | anotadores |
| `ds_classification` | Clasificación por imagen (clima/escena/franja/geometría) | classify | — |
| `ds_identity` | **Restringido**: correspondencia seudónimo ↔ identidad real | — (humano auditado) | — (ninguna `sa-dl-*`) |

## Diseño de escritura: `MERGE` (upsert idempotente)

Todos los jobs escriben con **`MERGE` vía query job** (las filas van como parámetro
`ARRAY<STRUCT>` con `UNNEST`), **no** con `load_table_from_json` (load job). Implicaciones:

- **Idempotente por clave** (`image_id`, o `dataset,version`): reprocesar/reejecutar no
  duplica filas, solo actualiza.
- Solo requiere **`roles/bigquery.jobUser`** (proyecto) + lectura/escritura de la tabla
  destino por recurso (`dlBqTableReader`/`dlBqTableWriter`). **No** necesita
  `bigquery.tables.create` (que solo existe a nivel **dataset** y rompería la
  granularidad por tabla). Añadir una columna = añadirla a la tabla y poblarla en el
  job; el código de escritura no cambia.
- Los valores `TIMESTAMP` se pasan como `datetime` (no string). Las filas se mandan por
  lotes (`BATCH_SIZE=1000`).

---

## `ds_raw_metadata`

### `tbl_images` — 1 fila/imagen
Partición `DATE(ingested_at)`, cluster `source,dataset,user_id`.
Escriben **ingest-public** (públicos) y **privacy** (usuario, source='user'); lee **classify**.

| Columna | Tipo | Modo | Descripción |
|---|---|---|---|
| `image_id` | STRING | R | Identificador único (clave) |
| `source` | STRING | R | `public` \| `user` |
| `dataset` | STRING | N | Dataset de origen (`culane`…) o `user` |
| `gcs_uri` | STRING | R | Ruta gs:// de la imagen (normalizada / limpia) |
| `width` | INTEGER | N | Ancho (px) |
| `height` | INTEGER | N | Alto (px) |
| `frame_id` | STRING | N | Identificador del frame |
| `sequence_id` | STRING | N | Sesión/tramo (`session_id` del origen) |
| `user_id` | STRING | N | Seudónimo `usr_<hash>`; solo imágenes de usuario |
| `captured_at` | TIMESTAMP | N | Fecha de captura (de `meta.json` en usuario) |
| `ingested_at` | TIMESTAMP | R | Fecha de ingesta |
| `split` | STRING | N | `train` \| `val` \| `test` (público; NULL en usuario) |

### `tbl_source_datasets` — 1 fila/versión de dataset público
Escribe **ingest-public**.

| Columna | Tipo | Modo | Descripción |
|---|---|---|---|
| `dataset` | STRING | R | Nombre del dataset |
| `version` | STRING | R | Versión/snapshot |
| `license` | STRING | N | Licencia |
| `num_images` | INTEGER | N | Nº de imágenes |
| `gcs_prefix` | STRING | N | Prefijo gs:// del normalizado |
| `normalized_at` | TIMESTAMP | N | Fecha de normalización |
| `notes` | STRING | N | Notas libres |

### `tbl_user_images_privacy` — auditoría de anonimización
Partición `DATE(processed_at)`. Escribe **privacy** (1 fila/imagen procesada).

| Columna | Tipo | Modo | Descripción |
|---|---|---|---|
| `image_id` | STRING | R | Imagen procesada (clave) |
| `raw_gcs_uri` | STRING | N | Ruta del crudo (efímero) |
| `clean_gcs_uri` | STRING | R | Ruta de la imagen limpia |
| `faces_blurred` | INTEGER | N | Nº de caras difuminadas |
| `plates_blurred` | INTEGER | N | Nº de matrículas difuminadas |
| `processed_at` | TIMESTAMP | R | Fecha de anonimización |
| `model_version` | STRING | N | Versión del anonimizador (= nombre del `.pt`, p. ej. `dashcam-anon-yolov8-v1`) |

> Idempotencia de privacy: el job lista el bucket crudo y hace **anti-join** contra esta
> tabla (`processed_at` últimas 48 h) para no reprocesar.

## `ds_label_review`

### `tbl_label_review_status` — cola/estado de revisión
Partición `DATE(created_at)`, cluster `status`. Escribe **privacy** *(pendiente, ver
README → Mejoras futuras)*.

| Columna | Tipo | Modo | Descripción |
|---|---|---|---|
| `image_id` | STRING | R | Imagen en revisión (clave) |
| `lines_gcs_uri` | STRING | R | Ruta del `.json` de líneas |
| `status` | STRING | R | `pending` \| `in_review` \| `reviewed` \| `rejected` |
| `confidence` | FLOAT | N | Confianza del modelo edge |
| `num_lines` | INTEGER | N | Nº de líneas predichas |
| `created_at` | TIMESTAMP | R | Entrada en la cola |
| `reviewed_at` | TIMESTAMP | N | Fecha de revisión |
| `reviewer` | STRING | N | Anotador que revisó |

## `ds_classification`

### `tbl_classifications` — clima/escena/franja/geometría
Partición `DATE(classified_at)`, cluster `road_geometry,weather`. Escribe **classify**.

| Columna | Tipo | Modo | Descripción |
|---|---|---|---|
| `image_id` | STRING | R | Imagen clasificada (clave) |
| `weather` | STRING | N | Clima detectado |
| `scene` | STRING | N | Tipo de escena |
| `timeofday` | STRING | N | Franja horaria |
| `road_geometry` | STRING | N | `curve` \| `straight` |
| `model` | STRING | N | Modelo usado (p. ej. `gemini-2.5-flash-lite`) |
| `scores` | JSON | N | Confianzas/puntuaciones por campo |
| `classified_at` | TIMESTAMP | R | Fecha de clasificación |
| `geometry_at` | TIMESTAMP | N | Fecha de cálculo de geometría |

> classify selecciona lo no clasificado vía `LEFT JOIN` contra esta tabla (idempotente).

## `ds_identity` — frontera de reidentificación (restringido)

Dataset aparte que **ninguna `sa-dl-*` puede leer**; solo un humano con permiso
explícito (auditado). Es lo que sostiene la seudonimización: el pipeline solo ve
`user_id` opacos.

### `tbl_user_identity` — 1 fila por usuario

| Columna | Tipo | Modo | Descripción |
|---|---|---|---|
| `user_id` | STRING | R | Seudónimo (= el de las rutas y `tbl_images`) |
| `real_ref` | STRING | R | Referencia real (email/id de la app) |
| `created_at` | TIMESTAMP | R | Alta de la correspondencia |
| `revoked_at` | TIMESTAMP | N | Baja / derecho al olvido (CCPA) |
