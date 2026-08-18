import os
import uuid

import boto3
import psycopg
import redis
from botocore.client import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pipeline:pipeline@localhost:5432/pipeline")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "images")
RESIZE_QUEUE = "resize-queue"

app = FastAPI()

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
