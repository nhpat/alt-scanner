import argparse
import concurrent.futures
import io
import time

import requests
from PIL import Image


def build_test_image(size):
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color="orange").save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def upload_one(api_url, image_bytes, index):
    start = time.time()
    try:
        response = requests.post(
            f"{api_url}/upload",
            files={"file": (f"loadtest-{index}.jpg", image_bytes, "image/jpeg")},
            timeout=30,
        )
        response.raise_for_status()
        return {"ok": True, "job_id": response.json()["job_id"], "elapsed": time.time() - start}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "elapsed": time.time() - start}


def main():
    parser = argparse.ArgumentParser(description="Fire N concurrent uploads at the image pipeline API")
    parser.add_argument("--url", default="http://localhost:8080", help="API base URL")
    parser.add_argument("--count", type=int, default=50, help="Total number of uploads")
    parser.add_argument("--concurrency", type=int, default=20, help="Concurrent workers")
    parser.add_argument("--image-size", type=int, default=3000, help="Test image dimension in pixels")
    args = parser.parse_args()

    print(f"Generating {args.image_size}x{args.image_size} test image...")
    image_bytes = build_test_image(args.image_size)

    print(f"Firing {args.count} uploads at {args.url} with concurrency {args.concurrency}...")
    start = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(upload_one, args.url, image_bytes, i) for i in range(args.count)]
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            results.append(result)
            status = "ok" if result["ok"] else f"FAILED ({result['error']})"
            print(f"[{i}/{args.count}] {status} ({result['elapsed']:.2f}s)")

    elapsed = time.time() - start
    succeeded = sum(1 for r in results if r["ok"])
    failed = len(results) - succeeded
    print(f"\nDone in {elapsed:.1f}s -- {succeeded} succeeded, {failed} failed")


if __name__ == "__main__":
    main()
