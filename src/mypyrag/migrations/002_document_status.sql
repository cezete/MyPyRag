ALTER TABLE {schema}.documents
    ALTER COLUMN chunking_fingerprint DROP NOT NULL,
    ALTER COLUMN embedding_model DROP NOT NULL,
    ALTER COLUMN embedding_model_digest DROP NOT NULL,
    ALTER COLUMN embedding_vector_size DROP NOT NULL,
    ALTER COLUMN indexed_at DROP NOT NULL;

ALTER TABLE {schema}.documents
    ADD COLUMN status text NOT NULL DEFAULT 'ready'
        CHECK (status IN ('queued', 'processing', 'ready', 'failed')),
    ADD COLUMN source_path text,
    ADD COLUMN last_error text,
    ADD COLUMN processing_started_at timestamptz;

UPDATE {schema}.documents
SET source_path = 'docs/IN/' || replace(universe, '.', '/') || '/' || original_filename
WHERE source_path IS NULL;

ALTER TABLE {schema}.documents ALTER COLUMN source_path SET NOT NULL;

CREATE INDEX documents_status_idx ON {schema}.documents (status);
CREATE INDEX documents_source_path_idx ON {schema}.documents (source_path text_pattern_ops);
