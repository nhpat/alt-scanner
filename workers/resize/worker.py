import io
import os

import boto3
import psycopg
import redis
from botocore.client import Config
from PIL import Image

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pipeline:pipeline@localhost:5432/pipeline")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "images")

RESIZE_QUEUE = "resize-queue"
CAPTION_QUEUE = "caption-queue"
RESIZE_MAX_DIM = 800
THUMBNAIL_SIZE = (150, 150)

s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
    config=Config(signature_version="s3v4"),
    region_name="us-east-1",
)

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def get_db():
    return psycopg.connect(DATABASE_URL)


def fetch_job(job_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT original_filename, original_object_key FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"job {job_id} not found")
    return {"original_filename": row[0], "original_object_key": row[1]}


def set_status(job_id, status, **fields):
    columns = ["status"]
    values = [status]
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    set_clause = ", ".join(f"{col} = %s" for col in columns)
    values.append(job_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE jobs SET {set_clause}, updated_at = now() WHERE id = %s",
            values,
        )


def build_variant(image, size, key_suffix, original_key, content_type, fit=False):
    variant = image.copy()
    if fit:
        variant.thumbnail(size)
    else:
        variant.thumbnail((size, size))

    buffer = io.BytesIO()
    fmt = image.format or "JPEG"
    variant.convert("RGB").save(buffer, format=fmt)
    buffer.seek(0)

    key = f"{key_suffix}/{original_key.split('/', 1)[1]}"
    s3.put_object(Bucket=MINIO_BUCKET, Key=key, Body=buffer.getvalue(), ContentType=content_type)
    return key


def process_job(job_id):
    job = fetch_job(job_id)
    set_status(job_id, "resizing")

    obj = s3.get_object(Bucket=MINIO_BUCKET, Key=job["original_object_key"])
    image = Image.open(io.BytesIO(obj["Body"].read()))
    image.load()
    content_type = obj.get("ContentType") or "image/jpeg"

    resized_key = build_variant(image, RESIZE_MAX_DIM, "resized", job["original_object_key"], content_type)
    thumbnail_key = build_variant(image, THUMBNAIL_SIZE, "thumbnail", job["original_object_key"], content_type, fit=True)

    set_status(
        job_id,
        "resized",
        resized_object_key=resized_key,
        thumbnail_object_key=thumbnail_key,
    )
    redis_client.lpush(CAPTION_QUEUE, job_id)
    print(f"[resize-worker] job {job_id} resized ok")


def main():
    print("[resize-worker] waiting for jobs on resize-queue")
    while True:
        _, job_id = redis_client.blpop(RESIZE_QUEUE, timeout=0)
        try:
            process_job(job_id)
        except Exception as exc:
            print(f"[resize-worker] job {job_id} failed: {exc}")
            try:
                set_status(job_id, "resize_failed", error_message=str(exc))
            except Exception as update_exc:
                print(f"[resize-worker] failed to record failure for {job_id}: {update_exc}")


if __name__ == "__main__":
    main()
