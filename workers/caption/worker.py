import base64
import os
import time

import anthropic
import boto3
import psycopg
import redis
from botocore.client import Config

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pipeline:pipeline@localhost:5432/pipeline")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "images")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

CAPTION_QUEUE = "caption-queue"
MAX_ATTEMPTS = 3

s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
    config=Config(signature_version="s3v4"),
    region_name="us-east-1",
)

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
anthropic_client = anthropic.Anthropic()


def get_db():
    return psycopg.connect(DATABASE_URL)


def fetch_job(job_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT resized_object_key FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"job {job_id} has no resized image")
    return row[0]


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


def caption_with_retry(image_b64, media_type):
    delay = 1
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = anthropic_client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=100,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                        },
                        {"type": "text", "text": "Describe this image in one concise sentence."},
                    ],
                }],
            )
            return next(block.text for block in response.content if block.type == "text")
        except anthropic.RateLimitError:
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2


def process_job(job_id):
    resized_key = fetch_job(job_id)
    set_status(job_id, "captioning")

    obj = s3.get_object(Bucket=MINIO_BUCKET, Key=resized_key)
    image_bytes = obj["Body"].read()
    media_type = obj.get("ContentType") or "image/jpeg"
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    caption = caption_with_retry(image_b64, media_type)

    set_status(job_id, "done", caption=caption)
    print(f"[caption-worker] job {job_id} captioned ok")


def main():
    print("[caption-worker] waiting for jobs on caption-queue")
    while True:
        _, job_id = redis_client.blpop(CAPTION_QUEUE, timeout=0)
        try:
            process_job(job_id)
        except Exception as exc:
            print(f"[caption-worker] job {job_id} failed: {exc}")
            try:
                set_status(job_id, "caption_failed", error_message=str(exc))
            except Exception as update_exc:
                print(f"[caption-worker] failed to record failure for {job_id}: {update_exc}")


if __name__ == "__main__":
    main()
