CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    original_filename TEXT NOT NULL,
    original_object_key TEXT NOT NULL,
    resized_object_key TEXT,
    thumbnail_object_key TEXT,
    caption TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    -- values: pending, resizing, resized, captioning, done, resize_failed, caption_failed
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    root_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scanning',
    -- values: scanning, done, failed
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    images_found INTEGER NOT NULL DEFAULT 0,
    crawl_done BOOLEAN NOT NULL DEFAULT false,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS image_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scan_id UUID NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    page_url TEXT NOT NULL,
    image_url TEXT NOT NULL,
    current_alt TEXT,
    context_snippet TEXT,
    suggested_alt TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    -- values: pending, done, failed
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
