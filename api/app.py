import csv
import io
import json
import os
import uuid
from urllib.parse import urlparse

import boto3
import psycopg
import redis
from botocore.client import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pipeline:pipeline@localhost:5432/pipeline")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "images")
RESIZE_QUEUE = "resize-queue"
CRAWL_QUEUE = "crawl-queue"
ALLOWED_SCHEMES = {"http", "https"}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
    config=Config(signature_version="s3v4"),
    region_name="us-east-1",
)

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)


@app.on_event("startup")
def ensure_bucket():
    try:
        s3.head_bucket(Bucket=MINIO_BUCKET)
    except ClientError:
        s3.create_bucket(Bucket=MINIO_BUCKET)


def get_db():
    return psycopg.connect(DATABASE_URL)


class ScanRequest(BaseModel):
    url: str


@app.post("/upload")
async def upload(file: UploadFile):
    job_id = str(uuid.uuid4())
    object_key = f"original/{job_id}/{file.filename}"

    body = await file.read()
    s3.put_object(Bucket=MINIO_BUCKET, Key=object_key, Body=body, ContentType=file.content_type)

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, original_filename, original_object_key, status)
            VALUES (%s, %s, %s, 'pending')
            """,
            (job_id, file.filename, object_key),
        )

    redis_client.lpush(RESIZE_QUEUE, job_id)

    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, original_filename, status, caption,
                   original_object_key, resized_object_key, thumbnail_object_key,
                   error_message, created_at, updated_at
            FROM jobs WHERE id = %s
            """,
            (job_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="job not found")

    columns = [
        "id", "original_filename", "status", "caption",
        "original_object_key", "resized_object_key", "thumbnail_object_key",
        "error_message", "created_at", "updated_at",
    ]
    return JSONResponse(dict(zip(columns, [str(v) if v is not None else None for v in row])))


VARIANT_COLUMNS = {
    "original": "original_object_key",
    "resized": "resized_object_key",
    "thumbnail": "thumbnail_object_key",
}


@app.get("/jobs/{job_id}/image/{variant}")
def get_image(job_id: str, variant: str):
    column = VARIANT_COLUMNS.get(variant)
    if column is None:
        raise HTTPException(status_code=400, detail="variant must be one of: original, resized, thumbnail")

    with get_db() as conn:
        row = conn.execute(f"SELECT {column} FROM jobs WHERE id = %s", (job_id,)).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    object_key = row[0]
    if object_key is None:
        raise HTTPException(status_code=404, detail=f"{variant} image not available yet")

    try:
        obj = s3.get_object(Bucket=MINIO_BUCKET, Key=object_key)
    except ClientError:
        raise HTTPException(status_code=404, detail="image not found in storage")

    return StreamingResponse(obj["Body"], media_type=obj.get("ContentType") or "image/jpeg")


@app.get("/jobs")
def list_jobs():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, original_filename, status, created_at
            FROM jobs ORDER BY created_at DESC LIMIT 50
            """
        ).fetchall()

    columns = ["id", "original_filename", "status", "created_at"]
    return [dict(zip(columns, [str(v) if v is not None else None for v in row])) for row in rows]


@app.delete("/jobs")
def clear_jobs():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT original_object_key, resized_object_key, thumbnail_object_key FROM jobs"
        ).fetchall()
        conn.execute("DELETE FROM jobs")

    keys = [key for row in rows for key in row if key]
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        s3.delete_objects(Bucket=MINIO_BUCKET, Delete={"Objects": [{"Key": k} for k in batch]})

    return {"deleted": len(rows)}


@app.post("/scans")
def create_scan(scan: ScanRequest):
    url = scan.url
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        raise HTTPException(status_code=400, detail="url must be an absolute http(s) URL")

    scan_id = str(uuid.uuid4())
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO scans (id, root_url, status, pages_scanned, images_found, crawl_done)
            VALUES (%s, %s, 'scanning', 0, 0, false)
            """,
            (scan_id, url),
        )
    redis_client.lpush(CRAWL_QUEUE, json.dumps({"scan_id": scan_id, "root_url": url}))

    return {"scan_id": scan_id}


@app.get("/scans")
def list_scans():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, root_url, status, pages_scanned, images_found, created_at
            FROM scans ORDER BY created_at DESC LIMIT 200
            """
        ).fetchall()

    columns = ["id", "root_url", "status", "pages_scanned", "images_found", "created_at"]
    return [dict(zip(columns, [str(v) if v is not None else None for v in row])) for row in rows]


@app.delete("/scans")
def clear_scans():
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        conn.execute("DELETE FROM scans")  # image_findings cascade-deletes via FK

    return {"deleted": count}


@app.get("/scans/{scan_id}")
def get_scan(scan_id: str):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, root_url, status, pages_scanned, images_found, error_message, created_at, updated_at
            FROM scans WHERE id = %s
            """,
            (scan_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="scan not found")

        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM image_findings WHERE scan_id = %s GROUP BY status",
            (scan_id,),
        ).fetchall())

    columns = ["id", "root_url", "status", "pages_scanned", "images_found", "error_message", "created_at", "updated_at"]
    result = dict(zip(columns, [str(v) if v is not None else None for v in row]))
    result["findings_pending"] = counts.get("pending", 0)
    result["findings_done"] = counts.get("done", 0)
    result["findings_failed"] = counts.get("failed", 0)
    return result


@app.get("/scans/{scan_id}/findings")
def list_findings(scan_id: str):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, page_url, image_url, current_alt, suggested_alt, context_snippet, status, error_message
            FROM image_findings WHERE scan_id = %s ORDER BY created_at
            """,
            (scan_id,),
        ).fetchall()

    columns = ["id", "page_url", "image_url", "current_alt", "suggested_alt", "context_snippet", "status", "error_message"]
    return [dict(zip(columns, row)) for row in rows]


@app.get("/scans/{scan_id}/report.csv")
def download_report(scan_id: str):
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM scans WHERE id = %s", (scan_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="scan not found")
        rows = conn.execute(
            """
            SELECT page_url, image_url, current_alt, suggested_alt, context_snippet, status
            FROM image_findings WHERE scan_id = %s ORDER BY created_at
            """,
            (scan_id,),
        ).fetchall()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["page_url", "image_url", "current_alt", "suggested_alt", "context_snippet", "status"])
    writer.writerows(rows)
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=alt-text-report-{scan_id}.csv"},
    )
