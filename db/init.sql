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
