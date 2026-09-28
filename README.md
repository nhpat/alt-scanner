# Alt-Text Scanner & Image Processing Pipeline

An AI-powered accessibility scanner that crawls a website, finds images with missing or low-quality `alt` text, and suggests context-aware replacements using Claude vision — built on top of a containerized, Kubernetes-deployed async image processing pipeline. In a 60-upload burst, the resize-worker HPA scaled from 1 to 2 pods under CPU pressure, and all 61 tracked jobs reached `done`. Demonstrates: Docker, Kubernetes, horizontal pod autoscaling (HPA), object storage, async job queues, SSRF-safe web crawling, and LLM API integration (Claude vision).

## What it does

**Alt-Text Scanner** (the primary feature): paste a URL, and it crawls the site (same-origin, robots.txt-respecting), finds `<img>` tags with missing or junk alt text (no `alt` attribute, filename-as-alt like `alt="IMG_2048"`, or alt text under 3 characters — while correctly leaving `alt=""` alone, since that's the standard way to mark an image as decorative), and asks Claude to write a real replacement using the surrounding page text as context. Results are viewable live as the scan runs and exportable as CSV.

**Image Processing Pipeline** (the original build, now a collapsible demo panel in the UI): upload an image, it gets resized + thumbnailed, and Claude captions it — this is the architecture pattern (queue-driven workers, status tracking, object storage) that the scanner's own workers reuse.

## Architecture

```
Alt-Text Scanner
────────────────
Client
  |
  v
POST /scans {url} ──► Postgres (creates scan row, status: scanning)
  |
  └──► Redis (LPUSH {scan_id, root_url} to crawl-queue)

Crawl Worker (BLPOP crawl-queue)
  |
  ├──► fetches robots.txt once, honors it for the whole crawl
  ├──► same-origin BFS from root_url (cap: 20 pages, 100 images)
  ├──► SSRF-safe fetch per page (blocks private/link-local IPs, incl. via redirects)
  ├──► parses <img> tags, flags missing/junk alt text, grabs surrounding text as context
  ├──► Postgres (one image_findings row per flagged image, status: pending)
  └──► Redis (LPUSH finding_id to alttext-queue) — repeats per page until crawl exhausted

Alt-Text Worker (BLPOP alttext-queue)
  |
  ├──► SSRF-safe image download (same guard as the crawler, applied per-image —
  |     a malicious page could point <img src> at an internal address even on an
  |     otherwise-legitimate site)
  ├──► Claude API (claude-haiku-4-5, vision) — suggests alt text from image + page context
  └──► Postgres (status: done, suggested_alt: "..."); finalizes the scan once the
       crawler has stopped discovering pages AND every finding is terminal


Image Processing Pipeline
──────────────────────────
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

Two independently-scaling worker deployments (image pipeline, on Kubernetes today):

- **Resize worker** — CPU-bound (Pillow resizing) → scales via HPA on CPU utilization
- **Caption worker** — I/O-bound (waiting on the Claude API) → runs at a fixed replica count; scaling on queue depth via KEDA is a noted future improvement

The alt-text scanner's `crawl-worker` and `alttext-worker` are the newer pieces and are **not yet in the Kubernetes manifests** — they currently only run via docker-compose. Porting them (and giving `alttext-worker` an HPA, since Claude-call latency makes it the most natural one to scale under a traffic spike) is the next infra step if this moves beyond local use.

## Security: SSRF guardrails

Both the crawler and the alt-text worker fetch URLs that ultimately come from attacker-influenceable input (a user-submitted root URL, and `<img src>` values from whatever HTML that site serves). Every fetch — page or image — goes through the same guard:

- Only `http`/`https` schemes are allowed.
- The hostname is resolved via DNS and every resolved IP is checked against `ipaddress.*.is_global` — private, loopback, link-local (including the `169.254.169.254` cloud metadata address), and reserved ranges are all rejected.
- Redirects are followed manually (not by the HTTP client) so each hop re-validates the target before connecting — a 200 response from a public host can't be used to bounce the request to an internal one.
- `robots.txt` is fetched once per crawl and honored for every page visited.

## Tech Stack

- **Language:** Python — FastAPI (API), plain scripts (workers)
- **Web crawling/parsing:** httpx (SSRF-safe fetch), BeautifulSoup (HTML parsing), `urllib.robotparser`
- **Object storage:** MinIO (S3-compatible)
- **Database:** Postgres
- **Queue:** Redis lists (`LPUSH` / `BLPOP`)
- **AI:** Claude API (`claude-haiku-4-5`, vision) via the `anthropic` Python SDK
- **Frontend:** vanilla HTML/CSS/JS, served via nginx
- **Local orchestration:** docker-compose
- **Container orchestration:** Kubernetes via `kind` (image pipeline only, see note above)
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
│   ├── caption/
│   │   ├── worker.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── crawl/
│   │   ├── worker.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── alttext/
│       ├── worker.py
│       ├── Dockerfile
│       └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   ├── config.js
│   └── Dockerfile
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

CREATE TABLE scans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    root_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scanning',
    -- scanning, done, failed
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    images_found INTEGER NOT NULL DEFAULT 0,
    crawl_done BOOLEAN NOT NULL DEFAULT false,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE image_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scan_id UUID NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    page_url TEXT NOT NULL,
    image_url TEXT NOT NULL,
    current_alt TEXT,
    context_snippet TEXT,
    suggested_alt TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    -- pending, done, failed
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

## API Endpoints

**Alt-text scanner**

- `POST /scans {"url": "..."}` — validates the URL, creates a scan row, pushes it to `crawl-queue`. Returns `{"scan_id": ...}` immediately; crawling happens asynchronously.
- `GET /scans` — lists the 50 most recent scans.
- `GET /scans/{id}` — scan status, pages/images counts, and a breakdown of finding statuses (pending/done/failed).
- `GET /scans/{id}/findings` — every flagged image for a scan: current alt, suggested alt, context, status.
- `GET /scans/{id}/report.csv` — the same findings as a downloadable CSV.

**Image processing pipeline**

- `POST /upload` — accepts a multipart image upload, uploads it to MinIO, creates a job row, pushes to `resize-queue`. Returns `{"job_id": ...}`.
- `GET /jobs/{id}` — returns job status, caption (if done), and object keys for the original/resized/thumbnail images.
- `GET /jobs` — lists the 50 most recent jobs.
- `GET /jobs/{id}/image/{variant}` — streams the original/resized/thumbnail image bytes.
- `DELETE /jobs` — deletes all job rows and their stored images from MinIO (used by the "Clear all" button in the demo UI to reset to an empty slate).

## Error Handling

- **Resize worker:** corrupt/unsupported images are caught per-job; the job is marked `resize_failed` with an `error_message` and the worker loop continues rather than crashing.
- **Caption worker:** the Claude API call is wrapped in a retry loop (up to 3 attempts, exponential backoff) for rate-limit (`429`) errors. On repeated failure the job is marked `caption_failed` rather than left stuck — the resized image and thumbnail are still usable even without a caption.
- **Crawl worker:** a page that fails to fetch (timeout, non-200, blocked by robots.txt, SSRF-rejected) is skipped and the crawl continues with the next queued link. If *every* page fails — including the root URL — the scan is marked `failed` with a descriptive error rather than silently reporting a misleading "done, 0 images found."
- **Alt-text worker:** same retry-on-429 pattern as the caption worker; a finding whose image fails to download or caption is marked `failed` with the error preserved, and the scan still finalizes once every other finding is terminal.
- All workers run with `PYTHONUNBUFFERED=1` so `print()` diagnostics actually reach `docker logs` / `kubectl logs` in real time, instead of sitting in Python's stdout buffer until the process exits.

## Setup

### Prerequisites

- Docker Desktop
- [`kind`](https://kind.sigs.k8s.io/) (for the Kubernetes path, image pipeline only)
- `kubectl`
- Python 3.12+
- An [Anthropic API key](https://console.anthropic.com/) with billing enabled (captioning/alt-text suggestions are a few thousandths of a cent per call, but the account needs an active payment method or credit balance)

### Local development (docker-compose)

1. Create a `.env` file in the project root:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
2. Build and start everything:
   ```
   docker compose up -d --build
   ```
3. Open the UI at [http://localhost:8080](http://localhost:8080) — the Alt-Text Scanner is front and center; the upload/job pipeline demo is tucked under the collapsible "Image Processing Pipeline (demo)" section.
4. Or drive it via the API directly:
   ```
   # scan a page
   curl -X POST -H "Content-Type: application/json" \
     -d '{"url":"https://example.com/"}' http://localhost:8000/scans

   # check progress / pull results
   curl http://localhost:8000/scans/<scan_id>
   curl http://localhost:8000/scans/<scan_id>/findings

   # or the original image pipeline
   curl -X POST -F "file=@your-image.jpg" http://localhost:8000/upload
   curl http://localhost:8000/jobs/<job_id>
   ```

### Kubernetes (kind)

This currently covers the image processing pipeline only (`api`, `resize-worker`, `caption-worker`) — the scanner's `crawl-worker`/`alttext-worker` don't have manifests yet.

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
