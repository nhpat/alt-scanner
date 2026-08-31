# Image Processing Pipeline

A containerized, Kubernetes-deployed image processing pipeline with async job queues and AI-generated captions. Demonstrates: Docker, Kubernetes, horizontal pod autoscaling (HPA), object storage, async job processing, and LLM API integration (Claude vision).

## Architecture

```
Client
  |
  v
API service (FastAPI) ──► MinIO (stores original image)
  |
  ├──► Postgres (creates job row, status: pending)
  |
  └──► Redis (LPUSH job ID to resize-queue)

Resize Worker (BLPOP resize-queue)
  |
  ├──► MinIO (downloads original, uploads resized + thumbnail)
  ├──► Postgres (status: resized)
  └──► Redis (LPUSH job ID to caption-queue)

Caption Worker (BLPOP caption-queue)
  |
  ├──► MinIO (downloads resized image)
  ├──► Claude API (claude-haiku-4-5, vision) — generates caption
  └──► Postgres (status: done, caption: "...")
```

Two independently-scaling worker deployments:

- **Resize worker** — CPU-bound (Pillow resizing) → scales via HPA on CPU utilization
- **Caption worker** — I/O-bound (waiting on the Claude API) → runs at a fixed replica count; scaling on queue depth via KEDA is a noted future improvement

## Tech Stack

- **Language:** Python — FastAPI (API), plain scripts (workers)
- **Object storage:** MinIO (S3-compatible)
- **Database:** Postgres
- **Queue:** Redis lists (`LPUSH` / `BLPOP`)
- **AI:** Claude API (`claude-haiku-4-5`, vision) via the `anthropic` Python SDK
- **Local orchestration:** docker-compose
- **Container orchestration:** Kubernetes via `kind`
- **Image processing:** Pillow

## Project Structure

```
image-processing-project/
├── api/
│   ├── app.py
│   ├── Dockerfile
│   └── requirements.txt
├── workers/
│   ├── resize/
│   │   ├── worker.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── caption/
│       ├── worker.py
│       ├── Dockerfile
│       └── requirements.txt
├── db/
│   └── init.sql
├── k8s/
│   ├── postgres.yaml
│   ├── redis.yaml
│   ├── minio.yaml
│   ├── configmap.yaml
│   ├── anthropic-secret.yaml.example
│   ├── api-deployment.yaml
│   ├── resize-worker-deployment.yaml
│   ├── resize-worker-hpa.yaml
│   ├── caption-worker-deployment.yaml
│   └── metrics-server.yaml
├── loadtest/
│   ├── batch_upload.py
│   └── requirements.txt
└── docker-compose.yml
```

## Data Model (Postgres)

```sql
CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    original_filename TEXT NOT NULL,
    original_object_key TEXT NOT NULL,
    resized_object_key TEXT,
    thumbnail_object_key TEXT,
    caption TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    -- pending, resizing, resized, captioning, done, resize_failed, caption_failed
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

## API Endpoints

- `POST /upload` — accepts a multipart image upload, uploads it to MinIO, creates a job row, pushes to `resize-queue`. Returns `{"job_id": ...}`.
- `GET /jobs/{id}` — returns job status, caption (if done), and object keys for the original/resized/thumbnail images.
- `GET /jobs` — lists the 50 most recent jobs.

## Error Handling

- **Resize worker:** corrupt/unsupported images are caught per-job; the job is marked `resize_failed` with an `error_message` and the worker loop continues rather than crashing.
- **Caption worker:** the Claude API call is wrapped in a retry loop (up to 3 attempts, exponential backoff) for rate-limit (`429`) errors. On repeated failure the job is marked `caption_failed` rather than left stuck — the resized image and thumbnail are still usable even without a caption.

## Setup

### Prerequisites

- Docker Desktop
- [`kind`](https://kind.sigs.k8s.io/)
- `kubectl`
- Python 3.12+
- An [Anthropic API key](https://console.anthropic.com/) with billing enabled (captioning is a few thousandths of a cent per call, but the account needs an active payment method or credit balance)

### Local development (docker-compose)

1. Create a `.env` file in the project root:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
2. Build and start everything:
   ```
   docker compose up -d --build
   ```
3. Upload a test image:
   ```
   curl -X POST -F "file=@your-image.jpg" http://localhost:8000/upload
   ```
4. Poll for the result:
   ```
   curl http://localhost:8000/jobs/<job_id>
   ```

### Kubernetes (kind)

1. Create the cluster and load the images built above:
   ```
   kind create cluster --name img-pipeline
   kind load docker-image image-processing-project-api:latest --name img-pipeline
   kind load docker-image image-processing-project-resize-worker:latest --name img-pipeline
   kind load docker-image image-processing-project-caption-worker:latest --name img-pipeline
   ```
2. Create your real Anthropic secret from the template (this file is gitignored):
   ```
   cp k8s/anthropic-secret.yaml.example k8s/anthropic-secret.yaml
   # edit k8s/anthropic-secret.yaml and paste in a real key
   ```
3. Apply the infra layer and wait for it to be ready:
   ```
   kubectl apply -f k8s/postgres.yaml -f k8s/redis.yaml -f k8s/minio.yaml
   kubectl wait --for=condition=ready pod -l app=postgres --timeout=90s
   kubectl wait --for=condition=ready pod -l app=redis --timeout=90s
   kubectl wait --for=condition=ready pod -l app=minio --timeout=90s
   ```
4. Apply config, secret, and the app deployments:
   ```
   kubectl apply -f k8s/configmap.yaml -f k8s/anthropic-secret.yaml \
     -f k8s/api-deployment.yaml -f k8s/resize-worker-deployment.yaml -f k8s/caption-worker-deployment.yaml
   ```
5. Deploy metrics-server (required for HPA on `kind`) and the resize-worker HPA:
   ```
   kubectl apply -f k8s/metrics-server.yaml
   kubectl wait --for=condition=available deployment/metrics-server -n kube-system --timeout=90s
   kubectl apply -f k8s/resize-worker-hpa.yaml
   ```
6. Port-forward the API and test:
   ```
   kubectl port-forward svc/api 8080:8000
   curl -X POST -F "file=@your-image.jpg" http://localhost:8080/upload
   ```

## Load Testing & Autoscaling Results

`loadtest/batch_upload.py` fires N concurrent uploads of a large (3000x3000) test image at the API, generating a burst of CPU-bound resize work.

```
.venv/bin/python loadtest/batch_upload.py --url http://localhost:8080 --count 60 --concurrency 30 --image-size 3000
```

All 60 uploads completed in 1.8s (the API just accepts and queues); the real load lands on the resize worker afterward as it works through the backlog.

**Observed HPA behavior** (`kubectl get hpa resize-worker-hpa -w`), target 65% CPU utilization on a 250m request:

| Time     | CPU        | Replicas |
|----------|------------|----------|
| t+0s     | 0%/65%     | 1        |
| t+20s    | **92%/65%** | 1 → **2** (scale-up triggered) |
| t+30s    | 0%/65%     | 2        |
| ...      | 0%/65%     | 2 (steady while backlog drains) |
| ~5 min later | 0%/65% | **2 → 1** (scale-down, after HPA's default stabilization window) |

All 61 jobs in the batch (60 load-test + 1 earlier manual test) reached `status: done` with real Postgres row counts confirming it:

```
 status | count
--------+-------
 done   |    61
```
This confirms the resize worker scales out under real CPU pressure and scales back in automatically once the queue drains — HPA's default 5-minute scale-down stabilization window is what keeps it from flapping back down immediately after the spike.
