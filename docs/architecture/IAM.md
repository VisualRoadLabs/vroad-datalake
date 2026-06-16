# IAM Reference

Este documento describe las cuentas de servicio (SAs) y los agentes de servicio
gestionados por Google que se usan en el Data Lake de Visual Road, y los roles
personalizados a los que se asocian.

## Projects & conventions

- **Proyecto Data Lake:** `vr-prj-prod-data-v1` (número de proyecto `993161378963`), región `us-central1`. Todas las SAs de runtime viven aquí.
- **Proyecto CI/CD:** `vr-prj-dev-cicd-v1` — aloja el Artifact Registry (repo `datalake`) y la SA de despliegue.
- **Nomenclatura:** las SAs de runtime son `sa-dl-<función>` (una por servicio, mínimo privilegio; el proyecto va en el email). Los roles personalizados son `dl<Camel>`.
- **Ámbito:** cada concesión es a **nivel de recurso**, salvo `dlVertexPredict` y `roles/bigquery.jobUser`, que son a **nivel de proyecto** (los modelos fundacionales de Gemini y los jobs de BigQuery no exponen IAM por recurso).

## Summary

| Service Account | Purpose | Used By | Resources Accessed | IAM Roles |
| --- | --- | --- | --- | --- |
| `sa-dl-privacy` | Anonimiza caras y matrículas (irreversible) | `job-prod-privacy-usc1` | `bkt-prod-raw-user-usc1` (r), `bkt-prod-user-usc1` (w); `tbl_images`, `tbl_user_images_privacy`, `tbl_label_review_status` (w); Gemini | dlGcsObjectReader, dlGcsObjectWriter, dlBqTableWriter, dlVertexPredict |
| `sa-dl-ingest-public` | Normaliza datasets públicos | `job-prod-ingest-public-usc1` | `bkt-prod-raw-public-usc1` (r), `bkt-prod-public-usc1` (w); `tbl_images`, `tbl_source_datasets` (w) | dlGcsObjectReader, dlGcsObjectWriter, dlBqTableWriter, `roles/bigquery.jobUser` (proyecto) |
| `sa-dl-classify` | Clasifica clima/escena/franja/geometría | `job-prod-classify-usc1` | `bkt-prod-public-usc1`, `bkt-prod-user-usc1` (r); `tbl_images` (r), `tbl_classifications` (w); Gemini | dlGcsObjectReader, dlBqTableReader, dlBqTableWriter, dlVertexPredict |
| `sa-dl-workflow` | Ejecuta jobs e invoca workflows | `wf-prod-classify-usc1`, `wf-prod-public-ingest-usc1` | `job-prod-ingest-public-usc1`, `job-prod-classify-usc1`; `wf-prod-classify-usc1` | dlRunJobExecutor, dlWorkflowsInvoker |
| `sa-dl-sched-privacy` | Dispara el job de privacy (cada hora) | `sched-prod-privacy-usc1` | `job-prod-privacy-usc1` | dlRunJobExecutor |
| `sa-dl-sched-classify` | Dispara el workflow de classify (a diario) | `sched-prod-user-classify-usc1` | `wf-prod-classify-usc1` | dlWorkflowsInvoker |
| `sa-dl-eventarc` | Entrega eventos de descriptor al workflow de ingesta | `evt-prod-public-ingest-usc1` | `wf-prod-public-ingest-usc1`; proyecto | dlWorkflowsInvoker, `roles/eventarc.eventReceiver` (proyecto) |
| `sa-cicd-deployer` | Construye, sube y despliega (CI/CD) | Triggers de Cloud Build | proyecto de datos (Run/Workflows/Scheduler), cada `sa-dl-*`, repo `datalake` | `roles/run.developer`, `roles/workflows.editor`, `roles/cloudscheduler.admin`, `roles/iam.serviceAccountUser` (por cada SA de runtime), `roles/artifactregistry.writer` (`datalake`) |
| Cloud Run service agent | Descarga las imágenes de los jobs al arrancar | todos los Cloud Run jobs | repo `datalake` (pull) | dlArDownloader (sobre `datalake`) |
| Cloud Storage service agent | Cifra/descifra CMEK y publica eventos de ingesta | GCS (buckets de usuario, notificación de raw-public) | `key-prod-dl-cmek`; `top-prod-ingest-signals` | `roles/cloudkms.cryptoKeyEncrypterDecrypter` (clave), `roles/pubsub.publisher` (topic) |

> Las SAs de runtime son `<name>@vr-prj-prod-data-v1.iam.gserviceaccount.com`.
> `sa-cicd-deployer` es `sa-cicd-deployer@vr-prj-dev-cicd-v1.iam.gserviceaccount.com`.

---

## Runtime service accounts

### sa-dl-privacy

**Purpose**

* Job de privacy: difumina de forma irreversible caras y matrículas de las imágenes de usuario y registra la auditoría de anonimización.

**Used By**

* Cloud Run job `job-prod-privacy-usc1` (imagen `privacy`).

**Resources Accessed**

* `bkt-prod-raw-user-usc1` — lectura (crudo de usuario con PII; efímero).
* `bkt-prod-user-usc1` — escritura (imágenes limpias + `.lines.json`).
* `tbl_images`, `tbl_user_images_privacy`, `tbl_label_review_status` — escritura.
* Gemini en Vertex AI (proyecto) — cuando `ANON_METHOD=gemini`.

**IAM Roles**

* `dlGcsObjectReader` sobre `bkt-prod-raw-user-usc1`
* `dlGcsObjectWriter` sobre `bkt-prod-user-usc1`
* `dlBqTableWriter` sobre `tbl_images`, `tbl_user_images_privacy`, `tbl_label_review_status`
* `dlVertexPredict` (proyecto)

---

### sa-dl-ingest-public

**Purpose**

* Job de ingesta: normaliza datasets públicos (CULane, CurveLanes, …) al layout común y registra los metadatos.

**Used By**

* Cloud Run job `job-prod-ingest-public-usc1` (imagen `ingest-public`).

**Resources Accessed**

* `bkt-prod-raw-public-usc1` — lectura (datasets crudos + `_descriptors/`).
* `bkt-prod-public-usc1` — escritura (imágenes normalizadas, `.lines.json`, `<split>.txt`).
* `tbl_images`, `tbl_source_datasets` — escritura (upsert por `image_id` / `dataset,version`).

**IAM Roles**

* `dlGcsObjectReader` sobre `bkt-prod-raw-public-usc1`
* `dlGcsObjectWriter` sobre `bkt-prod-public-usc1`
* `dlBqTableWriter` sobre `tbl_images`, `tbl_source_datasets`
* `roles/bigquery.jobUser` (**proyecto**) — necesario: el upsert carga las filas en una tabla temporal y ejecuta un `MERGE` (un load job + DML de BigQuery), que requiere permiso para lanzar jobs.

---

### sa-dl-classify

**Purpose**

* Job de clasificación: clima/escena/franja horaria/geometría de la vía por imagen, usando Gemini en Vertex.

**Used By**

* Cloud Run job `job-prod-classify-usc1` (imagen `classify`).

**Resources Accessed**

* `bkt-prod-public-usc1`, `bkt-prod-user-usc1` — lectura (imágenes a clasificar).
* `tbl_images` — lectura.
* `tbl_classifications` — escritura.
* Gemini en Vertex AI (proyecto).

**IAM Roles**

* `dlGcsObjectReader` sobre `bkt-prod-public-usc1`, `bkt-prod-user-usc1`
* `dlBqTableReader` sobre `tbl_images`
* `dlBqTableWriter` sobre `tbl_classifications`
* `dlVertexPredict` (proyecto)
* *(Si classify escribe con el mismo upsert de load-job/`MERGE`, también necesitará `roles/bigquery.jobUser` a nivel de proyecto.)*

---

### sa-dl-workflow

**Purpose**

* Identidad de los Cloud Workflows: ejecuta Cloud Run jobs e invoca otros workflows.

**Used By**

* `wf-prod-classify-usc1`, `wf-prod-public-ingest-usc1`.

**Resources Accessed**

* `job-prod-ingest-public-usc1`, `job-prod-classify-usc1` — ejecutar.
* `wf-prod-classify-usc1` — invocar (encadenado desde el workflow de ingesta pública).

**IAM Roles**

* `dlRunJobExecutor` sobre `job-prod-ingest-public-usc1`, `job-prod-classify-usc1`
* `dlWorkflowsInvoker` sobre `wf-prod-classify-usc1`

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

* `dlWorkflowsInvoker` sobre `wf-prod-classify-usc1`

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

* `dlWorkflowsInvoker` sobre `wf-prod-public-ingest-usc1`
* `roles/eventarc.eventReceiver` (proyecto)

---

## Deployment service account

### sa-cicd-deployer

**Purpose**

* Identidad de CI/CD (Cloud Build): pasa los tests, construye y sube las imágenes, y despliega/actualiza los Cloud Run jobs, Workflows y Schedulers.

**Used By**

* Triggers de Cloud Build (uno por servicio, filtrado por `includedFiles`). Vive en `vr-prj-dev-cicd-v1`.

**Resources Accessed**

* `vr-prj-prod-data-v1` — desplegar Cloud Run jobs, Workflows y Schedulers.
* Cada SA de runtime (`sa-dl-*`) — para adjuntarla al recurso que crea.
* Repo `datalake` del Artifact Registry — para subir las imágenes.

**IAM Roles**

* `roles/run.developer`, `roles/workflows.editor`, `roles/cloudscheduler.admin` (sobre `vr-prj-prod-data-v1`)
* `roles/iam.serviceAccountUser` sobre **cada** SA de runtime (para poder adjuntarla)
* `roles/artifactregistry.writer` sobre el repo `datalake` (para subir `<servicio>:$SHORT_SHA`)

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
- **Concesiones a nivel de proyecto** (las únicas excepciones al nivel de recurso): `dlVertexPredict` (Gemini), `roles/bigquery.jobUser` (load jobs / DML de BigQuery) y `roles/eventarc.eventReceiver`.
- La fuente de verdad de la infraestructura más amplia es `CLAUDE.md`; este fichero amplía sus secciones de IAM.
