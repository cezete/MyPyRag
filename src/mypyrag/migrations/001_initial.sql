CREATE TABLE {schema}.documents (
    document_id text PRIMARY KEY CHECK (document_id ~ '^[0-9a-f]{{64}}$'),
    original_filename text NOT NULL,
    source_relative_path text NOT NULL,
    source_sha256 text NOT NULL CHECK (source_sha256 = document_id),
    document_type text NOT NULL,
    universe text NOT NULL CHECK (length(btrim(universe)) > 0 AND universe <> 'n.a.'),
    docling_version text,
    chunking_fingerprint text NOT NULL,
    chunk_count integer NOT NULL CHECK (chunk_count >= 0),
    embedding_model text NOT NULL,
    embedding_model_digest text NOT NULL,
    embedding_vector_size integer NOT NULL CHECK (embedding_vector_size = 768),
    indexed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb
);

CREATE TABLE {schema}.chunks (
    chunk_id uuid PRIMARY KEY,
    document_id text NOT NULL REFERENCES {schema}.documents(document_id) ON DELETE CASCADE,
    chunk_index integer NOT NULL CHECK (chunk_index > 0),
    universe text NOT NULL CHECK (length(btrim(universe)) > 0 AND universe <> 'n.a.'),
    source_type text NOT NULL,
    text text NOT NULL CHECK (length(text) > 0),
    embedding_text text NOT NULL CHECK (length(embedding_text) > 0),
    text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{{64}}$'),
    embedding_text_sha256 text NOT NULL CHECK (embedding_text_sha256 ~ '^[0-9a-f]{{64}}$'),
    embedding_input_sha256 text NOT NULL CHECK (embedding_input_sha256 ~ '^[0-9a-f]{{64}}$'),
    original_filename text NOT NULL,
    document_type text NOT NULL,
    headings jsonb NOT NULL DEFAULT '[]'::jsonb,
    structural_path jsonb NOT NULL DEFAULT '[]'::jsonb,
    page_numbers jsonb NOT NULL DEFAULT '[]'::jsonb,
    docling_references jsonb NOT NULL DEFAULT '[]'::jsonb,
    table_metadata jsonb,
    chunking_metadata jsonb NOT NULL,
    embedding_model text NOT NULL,
    embedding_model_digest text NOT NULL,
    embedding_vector_size integer NOT NULL CHECK (embedding_vector_size = 768),
    embedding vector(768) NOT NULL,
    created_at timestamptz NOT NULL,
    indexed_at timestamptz NOT NULL,
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX chunks_document_id_idx ON {schema}.chunks (document_id);
CREATE INDEX chunks_universe_idx ON {schema}.chunks (universe);
CREATE INDEX chunks_universe_document_idx ON {schema}.chunks (universe, document_id);
CREATE INDEX chunks_embedding_hnsw_idx ON {schema}.chunks
    USING hnsw (embedding vector_cosine_ops);
