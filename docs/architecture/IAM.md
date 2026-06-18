# IAM Reference

Este documento describe las cuentas de servicio (SAs) y los agentes de servicio
gestionados por Google que se usan en el Data Lake de Visual Road, y los roles
personalizados a los que se asocian. La fuente de verdad más amplia es `CLAUDE.md`;
este fichero amplía sus secciones de IAM.

## Projects & conventions

- **Proyecto Data Lake:** `vr-prj-prod-data-v1` (número `993161378963`), región `us-central1`. Todas las SAs de runtime viven aquí.
- **Proyecto CI/CD:** `vr-prj-dev-cicd-v1` — aloja el Artifact Registry (repo `datalake`) y la SA de despliegue.
- **Nomenclatura:** las SAs de runtime son `sa-dl-<función>` (una por servicio, mínimo privilegio; el proyecto va en el email). Los roles personalizados son `dl<Camel>`.
- **Ámbito:** cada concesión es a **nivel de recurso**, salvo las que solo existen a nivel de proyecto: `dlVertexPredict`, `roles/bigquery.jobUser`, `roles/logging.logWriter`, `dlWorkflowsInvoker` y `roles/eventarc.eventReceiver`.
- **Escritura en BigQuery = `MERGE` (query job):** los jobs hacen upsert con un `MERGE` que pasa las filas como parámetro `ARRAY<STRUCT>` (`UNNEST`). **No** es un load job ni usa tablas temporales, así que **no** necesita `bigquery.tables.create` (que solo existe a nivel dataset); solo `roles/bigquery.jobUser` (proyecto) + lectura/escritura de la tabla destino por recurso.

## Summary

| Service Account | Purpose | Used By | Resources Accessed | IAM Roles |
| --- | --- | --- | --- | --- |
| `sa-dl-privacy` | Anonimiza caras y matrículas (irreversible, detector YOLOv8 en CPU) | `job-prod-privacy-usc1` | `bkt-prod-raw-user-usc1` (r), `bkt-prod-user-usc1` (w); `tbl_user_images_privacy` (r/w), `tbl_images` (w) | dlGcsObjectReader, dlGcsObjectWriter, dlBqTableReader, dlBqTableWriter, `roles/bigquery.jobUser` (proyecto) |
| `sa-dl-ingest-public` | Normaliza datasets públicos | `job-prod-ingest-public-usc1` | `bkt-prod-raw-public-usc1` (r), `bkt-prod-public-usc1` (w); `tbl_images`, `tbl_source_datasets` (r/w) | dlGcsObjectReader, dlGcsObjectWriter, dlBqTableReader, dlBqTableWriter, `roles/bigquery.jobUser`, `roles/logging.logWriter` (proyecto) |
| `sa-dl-classify` | Clasifica clima/escena/franja/geometría (Gemini en Vertex) | `job-prod-classify-usc1` | `bkt-prod-public-usc1`, `bkt-prod-user-usc1` (r); `tbl_images`, `tbl_classifications` (r), `tbl_classifications` (w); Gemini | dlGcsObjectReader, dlBqTableReader, dlBqTableWriter, dlVertexPredict, `roles/bigquery.jobUser`, `roles/logging.logWriter` (proyecto) |
| `sa-dl-workflow` | Ejecuta jobs e invoca workflows | `wf-prod-classify-usc1`, `wf-prod-public-ingest-usc1` | `job-prod-ingest-public-usc1`, `job-prod-classify-usc1`; `wf-prod-classify-usc1` | dlRunJobExecutor, iam.serviceAccountUser (SAs de runtime), dlWorkflowsInvoker, run.viewer, `roles/logging.logWriter` |
| `sa-dl-sched-privacy` | Dispara el job de privacy (cada hora) | `sched-prod-privacy-usc1` | `job-prod-privacy-usc1` | dlRunJobExecutor |
| `sa-dl-sched-classify` | Dispara el workflow de classify (a diario) | `sched-prod-user-classify-usc1` | `wf-prod-classify-usc1` | dlWorkflowsInvoker |
| `sa-dl-eventarc` | Entrega eventos de descriptor al workflow de ingesta | `evt-prod-public-ingest-usc1` | `wf-prod-public-ingest-usc1`; proyecto | dlWorkflowsInvoker, `roles/eventarc.eventReceiver` (proyecto) |
| `sa-cicd-deployer` | Construye, sube y despliega (CI/CD); hornea el modelo de privacy | Triggers de Cloud Build | proyecto de datos (Run/Workflows/Scheduler), cada `sa-dl-*`, repo `datalake`, `bkt-prod-models-usc1` (r), logs de build | run.developer, workflows.editor, cloudscheduler.admin, iam.serviceAccountUser (cada SA runtime), artifactregistry.writer (`datalake`), dlGcsObjectReader (`bkt-prod-models-usc1`), logging.logWriter |
| Cloud Run service agent | Descarga las imágenes de los jobs al arrancar | todos los Cloud Run jobs | repo `datalake` (pull) | dlArDownloader (sobre `datalake`) |
| Cloud Storage service agent | Cifra/descifra CMEK y publica eventos de ingesta | GCS (buckets de usuario, notificación de raw-public) | `key-prod-dl-cmek`; `top-prod-ingest-signals` | `roles/cloudkms.cryptoKeyEncrypterDecrypter` (clave), `roles/pubsub.publisher` (topic) |

> Las SAs de runtime son `<name>@vr-prj-prod-data-v1.iam.gserviceaccount.com`.
> `sa-cicd-deployer` es `sa-cicd-deployer@vr-prj-dev-cicd-v1.iam.gserviceaccount.com`.

---

## Runtime service accounts

### sa-dl-privacy

**Purpose**

* Job de privacy: difumina de forma irreversible caras y matrículas de las imágenes de usuario con un detector **YOLOv8 en CPU** (`dashcam_anonymizer`, `ANON_METHOD=detector`), registra la auditoría y cataloga la imagen limpia. **No usa Gemini.**

**Used By**

* Cloud Run job `job-prod-privacy-usc1` (imagen `privacy`).

**Resources Accessed**

* `bkt-prod-raw-user-usc1` — lectura (crudo de usuario con PII; efímero).
* `bkt-prod-user-usc1` — escritura (imágenes limpias + `.lines.json`).
* `tbl_user_images_privacy` — lectura (anti-join idempotente) y escritura (auditoría).
* `tbl_images` — escritura (cataloga la imagen limpia como `source='user'`).

**IAM Roles**

* `dlGcsObjectReader` sobre `bkt-prod-raw-user-usc1`
* `dlGcsObjectWriter` sobre `bkt-prod-user-usc1`
* `dlBqTableReader` sobre `tbl_user_images_privacy`
* `dlBqTableWriter` sobre `tbl_user_images_privacy`, `tbl_images`
* `roles/bigquery.jobUser` (**proyecto**) — para el `MERGE`

**Notas**

* El modelo `.pt` se **hornea en la imagen** en build; en runtime la SA **no** accede a `bkt-prod-models-usc1`.
* **Sin** `dlVertexPredict` (no usa Gemini), **sin** rol de KMS (GCS cifra/descifra el CMEK), **sin** `logging.logWriter` (Cloud Run captura el stdout).
* No borra el crudo (no tiene permiso de delete); lo elimina el TTL 24 h del bucket.
* *(Futuro)* la cola de revisión añadiría `dlBqTableReader` + `dlBqTableWriter` sobre `tbl_label_review_status`.

---

### sa-dl-ingest-public

**Purpose**

* Job de ingesta: normaliza datasets públicos (CULane, CurveLanes, …) al layout común y registra los metadatos.

**Used By**

* Cloud Run job `job-prod-ingest-public-usc1` (imagen `ingest-public`).

**Resources Accessed**

* `bkt-prod-raw-public-usc1` — lectura (datasets crudos + `_descriptors/`).
* `bkt-prod-public-usc1` — escritura (imágenes normalizadas, `.lines.json`, `<split>.txt`).
* `tbl_images`, `tbl_source_datasets` — lectura/escritura (upsert por `image_id` / `dataset,version`).

**IAM Roles**

* `dlGcsObjectReader` sobre `bkt-prod-raw-public-usc1`
* `dlGcsObjectWriter` sobre `bkt-prod-public-usc1`
* `dlBqTableReader` + `dlBqTableWriter` sobre `tbl_images`, `tbl_source_datasets`
* `roles/bigquery.jobUser` (**proyecto**) — el `MERGE` es un query job
* `roles/logging.logWriter` (**proyecto**)

---

### sa-dl-classify

**Purpose**

* Job de clasificación: clima/escena/franja horaria/geometría de la vía, usando Gemini en Vertex.

**Used By**

* Cloud Run job `job-prod-classify-usc1` (imagen `classify`).

**Resources Accessed**

* `bkt-prod-public-usc1`, `bkt-prod-user-usc1` — lectura (imágenes a clasificar).
* `tbl_images` — lectura (anti-join de lo no clasificado).
* `tbl_classifications` — lectura (el `MERGE` consulta la tabla destino) y escritura.
* Gemini en Vertex AI (proyecto).

**IAM Roles**

* `dlGcsObjectReader` sobre `bkt-prod-public-usc1`, `bkt-prod-user-usc1`
* `dlBqTableReader` sobre `tbl_images`, `tbl_classifications`
* `dlBqTableWriter` sobre `tbl_classifications`
* `dlVertexPredict` (**proyecto**)
* `roles/bigquery.jobUser` (**proyecto**) — el `MERGE` es un query job
* `roles/logging.logWriter` (**proyecto**)

---

### sa-dl-workflow

**Purpose**

* Identidad de los Cloud Workflows: ejecuta Cloud Run jobs e invoca otros workflows, y espera a que el job termine.

**Used By**

* `wf-prod-classify-usc1`, `wf-prod-public-ingest-usc1`.

**Resources Accessed**

* `job-prod-ingest-public-usc1`, `job-prod-classify-usc1` — ejecutar.
* `sa-dl-ingest-public`, `sa-dl-classify` — adjuntar al ejecutar el job.
* `wf-prod-classify-usc1` — invocar (encadenado desde el workflow de ingesta pública).

**IAM Roles**

* `dlRunJobExecutor` sobre `job-prod-ingest-public-usc1`, `job-prod-classify-usc1`
* `roles/iam.serviceAccountUser` sobre `sa-dl-ingest-public`, `sa-dl-classify`
* `dlWorkflowsInvoker` (**proyecto**)
* `roles/run.viewer` (o `dlRunJobWatcher`) — leer el estado de las ejecuciones (Workflows espera al job)
* `roles/logging.logWriter` (**proyecto**)

---

### sa-dl-sched-privacy

**Purpose**

* Identidad del scheduler que dispara el job de privacy.

**Used By**

* Cloud Scheduler `sched-prod-privacy-usc1` (cron `0 * * * *`, cada hora).

**Resources Accessed**

* `job-prod-privacy-usc1` — ejecutar.

**IAM Roles**

* `dlRunJobExecutor` sobre `job-prod-privacy-usc1`

---

### sa-dl-sched-classify

**Purpose**

* Identidad del scheduler que dispara el workflow de classify.

**Used By**

* Cloud Scheduler `sched-prod-user-classify-usc1` (cron `0 0 * * *`, `America/Chicago`).

**Resources Accessed**

* `wf-prod-classify-usc1` — invocar.

**IAM Roles**

* `dlWorkflowsInvoker` (**proyecto**)

---

### sa-dl-eventarc

**Purpose**

* Identidad de Eventarc que entrega los eventos de subida de descriptor al workflow de ingesta pública.

**Used By**

* Trigger de Eventarc `evt-prod-public-ingest-usc1` (topic `top-prod-ingest-signals` → workflow).

**Resources Accessed**

* `wf-prod-public-ingest-usc1` — invocar.
* Proyecto — para recibir los eventos.

**IAM Roles**

* `dlWorkflowsInvoker` (**proyecto**)
* `roles/eventarc.eventReceiver` (**proyecto**)

---

## Deployment service account

### sa-cicd-deployer

**Purpose**

* Identidad de CI/CD (Cloud Build): pasa los tests, construye y sube las imágenes, hornea el modelo de privacy y despliega/actualiza los Cloud Run jobs, Workflows y Schedulers.

**Used By**

* Triggers de Cloud Build (uno por servicio, filtrado por `includedFiles`). Vive en `vr-prj-dev-cicd-v1`.

**Resources Accessed**

* `vr-prj-prod-data-v1` — desplegar Cloud Run jobs, Workflows y Schedulers.
* Cada SA de runtime (`sa-dl-*`) — para adjuntarla al recurso que crea.
* Repo `datalake` del Artifact Registry — para subir las imágenes.
* `bkt-prod-models-usc1` — lectura: el paso `fetch-model` baja el `.pt` al contexto de build (solo `_SERVICE=privacy`).
* `vr-prj-dev-cicd-v1` — escribir los logs de cada build.

**IAM Roles**

* `roles/run.developer`, `roles/workflows.editor`, `roles/cloudscheduler.admin` (sobre `vr-prj-prod-data-v1`)
* `roles/iam.serviceAccountUser` sobre **cada** SA de runtime (para poder adjuntarla)
* `roles/artifactregistry.writer` sobre el repo `datalake` (para subir `<servicio>:$SHORT_SHA`)
* `dlGcsObjectReader` sobre `bkt-prod-models-usc1` (hornear el modelo en el build)
* `roles/logging.logWriter` sobre `vr-prj-dev-cicd-v1` (Cloud Build necesita escribir sus logs)

---

## Google-managed service agents

Son identidades auto-creadas (no son `sa-dl-*`); solo les concedemos los roles de abajo.

### Cloud Run service agent

**Identity**

* `service-993161378963@serverless-robot-prod.iam.gserviceaccount.com` (proyecto de datos).

**Purpose**

* Descarga la imagen de contenedor de cada job desde el repo `datalake` al arrancar el job.

**IAM Roles**

* `dlArDownloader` sobre el repo `datalake` (en `vr-prj-dev-cicd-v1`).

---

### Cloud Storage service agent

**Identity**

* `service-993161378963@gs-project-accounts.iam.gserviceaccount.com` (proyecto de datos).

**Purpose**

* Cifra/descifra los buckets de usuario protegidos con CMEK y publica las notificaciones de subida de descriptor en Pub/Sub.

**IAM Roles**

* `roles/cloudkms.cryptoKeyEncrypterDecrypter` sobre `key-prod-dl-cmek` (keyring `kr-prod-dl-usc1`) — para que GCS pueda usar CMEK en `bkt-prod-raw-user-usc1` / `bkt-prod-user-usc1`.
* `roles/pubsub.publisher` sobre el topic `top-prod-ingest-signals` — para que la notificación de GCS sobre `bkt-prod-raw-public-usc1` (prefijo `_descriptors/`, `OBJECT_FINALIZE`) pueda publicar.

---

## Custom roles (least privilege)

Definidos en `vr-prj-prod-data-v1`:

| Role | Permissions |
| --- | --- |
| `dlGcsObjectReader` | `storage.objects.get`, `storage.objects.list` |
| `dlGcsObjectWriter` | `storage.objects.create/get/list` (sin borrar) |
| `dlBqTableReader` | `bigquery.tables.get`, `bigquery.tables.getData` |
| `dlBqTableWriter` | `bigquery.tables.get`, `bigquery.tables.updateData` |
| `dlVertexPredict` | `aiplatform.endpoints.predict` |
| `dlRunJobExecutor` | `run.jobs.run/get`, `run.executions.get` |
| `dlWorkflowsInvoker` | `workflows.workflows.get`, `workflows.executions.create` |

Definidos en `vr-prj-dev-cicd-v1`:

| Role | Permissions |
| --- | --- |
| `dlArDownloader` | `artifactregistry.repositories.downloadArtifacts`, `artifactregistry.dockerimages.get/list` |

---

## Notes

- **Aislamiento por servicio:** una SA por servicio, cada una accede solo a sus recursos, con permisos a nivel de recurso.
- **Concesiones a nivel de proyecto** (las únicas excepciones): `dlVertexPredict` (Gemini), `roles/bigquery.jobUser` (MERGE/query jobs), `roles/logging.logWriter`, `dlWorkflowsInvoker` (Workflows no expone IAM por recurso vía gcloud), `roles/eventarc.eventReceiver` y `run.viewer`/`dlRunJobWatcher`.
- **`MERGE` en vez de load job:** el upsert pasa las filas como `ARRAY<STRUCT>` y ejecuta un `MERGE` (query job). Por eso basta `bigquery.jobUser` + lectura/escritura de la tabla destino, sin `bigquery.tables.create` a nivel dataset. Ver `bigquery.md`.
- **privacy sin Gemini:** el job usa el detector local YOLOv8 (`ANON_METHOD=detector`), horneado en la imagen; por eso su SA **no** lleva `dlVertexPredict` y solo `sa-cicd-deployer` lee el bucket de modelos (en build).
