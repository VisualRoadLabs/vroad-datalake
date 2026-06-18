# Storage Reference (GCS buckets & layout)

Estructura de carpetas de los buckets de Cloud Storage del Data Lake de Visual Road
y las convenciones de nombres. La fuente de verdad más amplia es `CLAUDE.md`; este
documento detalla el layout interno de cada bucket.

Todos los buckets: región `us-central1`, clase **Standard**, **uniform bucket-level
access**, **public access prevention = enforced**.

## Buckets de datos (4)

| Bucket | Contenido | Cifrado | Ciclo de vida | Lee / Escribe |
|---|---|---|---|---|
| `bkt-prod-raw-public-usc1` | Datasets públicos crudos + descriptores | Google | — | ingest-public (r) |
| `bkt-prod-public-usc1` | Públicos normalizados (layout propio) | Google | — | ingest-public (w), classify (r) |
| `bkt-prod-raw-user-usc1` | Crudo de usuario (PII) | **CMEK** | **TTL 24 h** (delete age 1) | privacy (r) |
| `bkt-prod-user-usc1` | Usuario limpio (anonimizado) | **CMEK** | — | privacy (w), classify (r) |

Bucket de **assets de build** (no es un bucket de datos del pipeline):

| Bucket | Contenido | Quién accede |
|---|---|---|
| `bkt-prod-models-usc1` | Pesos de modelos `.pt`/ONNX (`privacy/`, `classify/`) | `sa-cicd-deployer` (r) **solo en build** |

> El modelo de privacy (`privacy/dashcam-anon-yolov8-v1.pt`) se **hornea en la imagen
> Docker** durante el build (paso `fetch-model` de `cloudbuild.yaml`). En runtime los
> jobs **no** acceden a este bucket: el modelo ya viaja dentro de la imagen en
> `/app/models/`.

## Convención de nombres

- **`user_id`**: seudónimo opaco `usr_<hash>`, nunca el identificador real (la
  correspondencia con la identidad real vive en `ds_identity`, ver `bigquery.md`).
- **`session_id`** (tramo, lo trae el origen): `<fechahoraUTC>__<user_id>__<corr>`,
  p. ej. `20260314T0830Z__usr_a1b2c3__01` (ISO básico sin `:` ni `-`, UTC,
  correlativo por si coinciden en el mismo minuto).
- La fecha para **filtrar** vive en BigQuery (`captured_at`), **no** en las rutas; el
  `session_id` solo lleva la fecha como ayuda de nombrado.

## `bkt-prod-raw-public-usc1` — públicos crudos

```
_descriptors/
  default.yml                 # plantilla
  culane.yml                  # <dataset>.yml -> dispara la ingesta de ese dataset
<dataset>/                    # carpeta de 1er nivel = nombre del dataset (culane, curvelanes, ...)
  ...                         # estructura nativa del dataset (sin tocar)
```

Subir `_descriptors/<dataset>.yml` (copia de `default.yml`) es el **último paso** que
arranca la ingesta: una notificación de GCS (`OBJECT_FINALIZE`, prefijo
`_descriptors/`) publica en Pub/Sub y Eventarc dispara el workflow.

## `bkt-prod-public-usc1` — públicos normalizados

Layout propio que produce el job **ingest-public** (común a todos los datasets):

```
<dataset>/
  <split>/                    # train | val | test
    images/<ruta_nativa>.jpg  # se preserva la estructura de carpetas nativa
    label/<ruta_nativa>.lines.json
    <split>.txt               # una ruta de imagen por linea (listado del split)
```

El split `test` de CULane va **dividido por categoría** (`test/<categoria>/images/...`,
p. ej. `test/curve/images/...`, `test/night/...`). El `.lines.json` normalizado es
estilo CurveLanes: `{"timestamp": <epoch>, "Lines": [[{"x":int,"y":int}, ...], ...]}`.

## `bkt-prod-raw-user-usc1` — crudo de usuario (PII)

```
<user_id>/<session_id>/
  meta.json                   # SOLO en crudo: fecha inicio/fin, nº frames, origen
  images/000001.jpg           # frames del dashcam (con PII: caras, matriculas)
  lines/000001.lines.json     # opcional: predicciones del modelo edge
```

CMEK + **TTL 24 h**: el crudo es efímero. El job de privacy lo lee, anonimiza y
escribe en el bucket limpio; **no borra** el crudo (no tiene permiso de delete) — lo
elimina el lifecycle del bucket.

## `bkt-prod-user-usc1` — usuario limpio (anonimizado)

```
<user_id>/<session_id>/
  images/000001.jpg           # MISMA ruta relativa, ya anonimizada (caras/matriculas difuminadas)
  lines/000001.lines.json     # copiado tal cual del crudo (si existia)
```

**Misma ruta relativa que el crudo, solo cambia el bucket** — así el job de privacy
solo cambia de bucket al escribir. El `meta.json` **no** se copia (se queda en crudo y
se consume para `captured_at`).

## `bkt-prod-models-usc1` — pesos de modelos (build-time)

```
privacy/
  dashcam-anon-yolov8-v1.pt   # detector de caras/matriculas (YOLOv8, dashcam_anonymizer)
classify/                     # futuro modelo propio de clasificacion
```

El mismo nombre versionado (`dashcam-anon-yolov8-v1`) viaja por tres sitios para
trazabilidad: el objeto en el bucket, la imagen Docker, y la columna `model_version`
de `tbl_user_images_privacy`.

## `image_id` (clave en BigQuery)

Derivado de la ruta, estable y reproducible (necesario para el `MERGE` idempotente):

- **Público:** `<dataset>/<ruta_relativa_sin_extension>`
  (p. ej. `culane/train/images/driver_23.../00780`).
- **Usuario:** `user/<user_id>/<session_id>/images/<frame_sin_extension>`
  (p. ej. `user/usr_a1b2c3/20260314T0830Z__usr_a1b2c3__01/images/000001`).

## Privacidad / seguridad

- **CMEK** en los buckets con PII (`raw-user`, `user`); la clave `key-prod-dl-cmek`
  (keyring `kr-prod-dl-usc1`) la usa el **agente de GCS**, no los jobs (ver `IAM.md`).
- Anonimización **irreversible** y crudo efímero (TTL 24 h): no se conservan copias
  con PII.
- Aislamiento por SA: cada job accede solo a sus buckets (ver `IAM.md`).
