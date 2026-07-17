-- ModelArk catalog schema (SQLite, WAL mode). The shared core (modelark.core), reusable by
-- future modelark modules. git-annex owns *where bytes physically live*;
-- the `replicas` table mirrors `git annex whereis` for offline-queryable location.
-- WAL journaling → concurrent readers + one writer across processes (DEC-024, replacing DuckDB's
-- single-writer lock). SQLite uses type affinity, while CHECK/FOREIGN KEY constraints below enforce
-- the durable identities, local enums, boolean domains, and non-negative byte/count invariants.

-- One row per Hugging Face repo we know about (downloaded or not).
CREATE TABLE IF NOT EXISTS models (
    repo_id            VARCHAR PRIMARY KEY NOT NULL CHECK (length(trim(repo_id)) > 0), -- "org/name"
    author             VARCHAR,
    model_name         VARCHAR,
    params_b           DOUBLE CHECK (params_b IS NULL OR params_b >= 0), -- billions (NULL if undeclared)
    architecture       VARCHAR,               -- e.g. LlamaForCausalLM
    modality           VARCHAR,               -- domain: text | vision | audio | multimodal | image-gen | video-gen
    category           VARCHAR,               -- generative-llm|encoder|seq2seq|translation|embedding|reranker|classifier|qa (DEC-002)
    variant            VARCHAR,               -- base | instruct | reasoning
    pipeline_tag       VARCHAR,               -- raw HF hint (unreliable; kept for reference)
    library            VARCHAR,               -- transformers | gguf | mlx | ...
    license            VARCHAR,
    gated              VARCHAR,               -- false | auto | manual
    private            BOOLEAN CHECK (private IS NULL OR private IN (0, 1)),
    likes              INTEGER CHECK (likes IS NULL OR likes >= 0),
    downloads_30d      INTEGER CHECK (downloads_30d IS NULL OR downloads_30d >= 0),
    downloads_all      BIGINT CHECK (downloads_all IS NULL OR downloads_all >= 0),
    trending_score     DOUBLE,
    tags               TEXT,                  -- JSON array (was DuckDB VARCHAR[]; json-encoded in Python)
    total_size_bytes   BIGINT CHECK (total_size_bytes IS NULL OR total_size_bytes >= 0),
    hf_last_modified   TIMESTAMP,
    hf_created_at      TIMESTAMP,
    status             VARCHAR NOT NULL DEFAULT 'discovered'
                       CHECK (status IN ('discovered','inspected','wishlist','fetching','archived','skip')),
    numcopies          INTEGER NOT NULL DEFAULT 1 CHECK (numcopies IN (1, 2)), -- DEC-014
    score              DOUBLE,
    notes              VARCHAR,
    discovered_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One row per file inside a repo.
CREATE TABLE IF NOT EXISTS files (
    repo_id      VARCHAR NOT NULL,
    rfilename    VARCHAR NOT NULL CHECK (length(rfilename) > 0),
    size_bytes   BIGINT CHECK (size_bytes IS NULL OR size_bytes >= 0),
    is_lfs       BOOLEAN CHECK (is_lfs IS NULL OR is_lfs IN (0, 1)),
    sha256       VARCHAR,                     -- LFS canonical hash (NULL for tiny git blobs)
    format       VARCHAR CHECK (format IS NULL OR format IN
                 ('safetensors','gguf','pytorch','onnx','mlx','aux','other')),
    quant        VARCHAR,                     -- Q4_K_M | bf16 | gptq | awq | ...
    quant_bits   DOUBLE CHECK (quant_bits IS NULL OR quant_bits >= 0), -- nominal bits-per-weight
    safety       VARCHAR CHECK (safety IS NULL OR safety IN ('safe','pickle','unknown')),
    PRIMARY KEY (repo_id, rfilename),
    FOREIGN KEY (repo_id) REFERENCES models(repo_id) ON UPDATE CASCADE ON DELETE CASCADE
);

-- The physical drive fleet (the "tape library").
CREATE TABLE IF NOT EXISTS drives (
    drive_label        VARCHAR PRIMARY KEY NOT NULL CHECK (length(trim(drive_label)) > 0),
    fs_uuid            VARCHAR,
    annex_uuid         VARCHAR,
    capacity_bytes     BIGINT CHECK (capacity_bytes IS NULL OR capacity_bytes >= 0),
    free_bytes         BIGINT CHECK (free_bytes IS NULL OR free_bytes >= 0),
    hw_model           VARCHAR,
    serial             VARCHAR,
    physical_location  VARCHAR,               -- "shelf box A, slot 3"
    role               VARCHAR NOT NULL DEFAULT 'primary' CHECK (role IN ('primary','replica')),
    raid_backed        BOOLEAN NOT NULL DEFAULT false CHECK (raid_backed IN (0, 1)),
    health             VARCHAR,
    last_seen          TIMESTAMP,
    notes              VARCHAR
);

-- Which file lives on which drive (mirror of `git annex whereis`).
CREATE TABLE IF NOT EXISTS replicas (
    repo_id      VARCHAR NOT NULL,
    rfilename    VARCHAR NOT NULL,
    drive_label  VARCHAR NOT NULL,
    annex_key    VARCHAR,
    present      BOOLEAN NOT NULL DEFAULT TRUE CHECK (present IN (0, 1)),
    verified_at  TIMESTAMP,
    added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo_id, rfilename, drive_label),
    FOREIGN KEY (repo_id, rfilename) REFERENCES files(repo_id, rfilename)
        ON UPDATE CASCADE ON DELETE CASCADE,
    FOREIGN KEY (drive_label) REFERENCES drives(drive_label)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

-- Verification results (one row per repo; re-runnable). Deliberately no models FK: `verify --repo`
-- supports inspecting an uncatalogued Hub repository before discovery.
CREATE TABLE IF NOT EXISTS verifications (
    repo_id          VARCHAR PRIMARY KEY NOT NULL,
    checksum_ok      BOOLEAN CHECK (checksum_ok IS NULL OR checksum_ok IN (0, 1)),
    structural_ok    BOOLEAN CHECK (structural_ok IS NULL OR structural_ok IN (0, 1)),
    shards_complete  BOOLEAN CHECK (shards_complete IS NULL OR shards_complete IN (0, 1)),
    format_safety    VARCHAR CHECK (format_safety IS NULL OR format_safety IN
                       ('safe','pickle-present','mixed','unknown')),
    pickle_scan      VARCHAR CHECK (pickle_scan IS NULL OR pickle_scan IN ('unscanned','n/a')),
    hf_scan_status   VARCHAR,   -- reserved for HF API integration; currently NULL
    signature        VARCHAR,   -- none | sigstore | gpg
    signer           VARCHAR,
    load_tier_max    VARCHAR CHECK (load_tier_max IS NULL OR load_tier_max = 'A'),
    functional_ok    BOOLEAN CHECK (functional_ok IS NULL OR functional_ok IN (0, 1)),
    detail           VARCHAR,   -- human-readable findings
    verified_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tool_versions    VARCHAR
);

-- The download set built in the portal; this IS the wishlist the fetch pipeline reads.
CREATE TABLE IF NOT EXISTS selection (
    repo_id      VARCHAR PRIMARY KEY NOT NULL,
    added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finalized_at TIMESTAMP,  -- NULL = still in the cart; set by "Finish" = committed wishlist
    FOREIGN KEY (repo_id) REFERENCES models(repo_id) ON UPDATE CASCADE ON DELETE CASCADE
);

-- What the fetch pipeline has archived, and where (the durable record of bytes on drives).
CREATE TABLE IF NOT EXISTS archived (
    repo_id      VARCHAR NOT NULL,
    rfilename    VARCHAR NOT NULL, -- original path within the HF repo
    stored_name  VARCHAR,          -- legacy basename (kept for migration/backward compatibility)
    stored_relpath VARCHAR,        -- POSIX path below <archive>/<repo_id>, including nested HF dirs
    drive_label  VARCHAR NOT NULL,
    orig_sha256  VARCHAR,          -- original-byte hash: HF-confirmed when supplied, else ingested sha256
    znn_sha256   VARCHAR,          -- compressed-blob hash (NULL when stored raw)
    orig_bytes   BIGINT CHECK (orig_bytes IS NULL OR orig_bytes >= 0),
    stored_bytes BIGINT CHECK (stored_bytes IS NULL OR stored_bytes >= 0),
    compressed   BOOLEAN NOT NULL CHECK (compressed IN (0, 1)),
    annex_key    VARCHAR,
    verified_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo_id, rfilename, drive_label),
    FOREIGN KEY (repo_id, rfilename) REFERENCES files(repo_id, rfilename)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    FOREIGN KEY (drive_label) REFERENCES drives(drive_label)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

-- Per-repo fetch outcomes — feeds the Download Status view (append-only, one row per attempt).
-- Deliberately no models FK: an explicit fetch can record a failed/not-found uncatalogued repo;
-- repo_id is also NULL for a plan-wide throttle event.
CREATE TABLE IF NOT EXISTS fetch_events (
    repo_id      VARCHAR,
    event_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    outcome      VARCHAR NOT NULL CHECK (outcome IN
                  ('archived','policy','auth','rate_limited','waiting','error','throttled',
                   'awaiting-drive','compress-fallback')),
    bytes        BIGINT CHECK (bytes IS NULL OR bytes >= 0),
    wait_seconds DOUBLE CHECK (wait_seconds IS NULL OR wait_seconds >= 0),
    detail       VARCHAR
);

-- A first-class Plan (#33, DEF-016) — one fill campaign's identity: a FIXED set of registered
-- drives (plan_drives) + the global selection/archived it fills + a capacity mode. The three
-- LIVE forecasts (raw/expected-stored/capacity, modelark.plan.totals) fuel the level-1
-- capacity failsafe. One plan for now (`ark`); selection/archived stay GLOBAL — a future plan_id
-- column on them is the multi-plan future (a DEF). Exactly one row has is_active (the current
-- backend/portal context); the #35 UI gate additionally forces an explicit operator pick per session.
CREATE TABLE IF NOT EXISTS plans (
    plan_id      VARCHAR PRIMARY KEY NOT NULL CHECK (length(trim(plan_id)) > 0),
    name         VARCHAR,
    annex_root   VARCHAR,                         -- the git-annex map root this plan fills
    capacity_mode VARCHAR NOT NULL DEFAULT 'guaranteed'
                  CHECK (capacity_mode IN ('guaranteed','compression_aware')),
    status       VARCHAR NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    is_active    BOOLEAN NOT NULL DEFAULT false CHECK (is_active IN (0, 1)),
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes        VARCHAR
);

-- The fixed drive set a plan fills — the ONLY capacity that exists for it (the failsafe boundary).
-- Populated at bootstrap (owns every registered drive) and by registration (#34) as drives are added.
CREATE TABLE IF NOT EXISTS plan_drives (
    plan_id      VARCHAR NOT NULL,
    drive_label  VARCHAR NOT NULL,
    added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plan_id, drive_label),
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON UPDATE CASCADE ON DELETE CASCADE,
    FOREIGN KEY (drive_label) REFERENCES drives(drive_label) ON UPDATE CASCADE ON DELETE CASCADE
);

-- Foreign-key child indexes keep parent updates/deletes and integrity checks bounded.
CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo_id);
CREATE INDEX IF NOT EXISTS idx_replicas_drive ON replicas(drive_label);
CREATE INDEX IF NOT EXISTS idx_archived_drive ON archived(drive_label);
CREATE INDEX IF NOT EXISTS idx_fetch_events_repo ON fetch_events(repo_id);
CREATE INDEX IF NOT EXISTS idx_plan_drives_drive ON plan_drives(drive_label);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_one_active ON plans(is_active) WHERE is_active = 1;

-- Denormalized model view for the portal/API (per-model full-precision size).
-- SQLite: GROUP BY the PK (repo_id) — bare m.* columns are functionally dependent on it; keep FILTER.
DROP VIEW IF EXISTS v_ui;
CREATE VIEW v_ui AS
SELECT m.repo_id, m.author, m.params_b, m.category, m.variant, m.license,
       m.downloads_30d,
       (m.gated NOT IN ('false','no') AND m.gated IS NOT NULL) AS gated,
       coalesce(sum(f.size_bytes) FILTER (WHERE f.format='safetensors'),
                sum(f.size_bytes), 0) AS bytes
FROM models m LEFT JOIN files f USING (repo_id)
WHERE m.status != 'skip'
GROUP BY m.repo_id;

-- Rollups. list_distinct(list(x)) → group_concat(DISTINCT x) (comma string; group_concat skips NULLs).
DROP VIEW IF EXISTS v_model_summary;
CREATE VIEW v_model_summary AS
SELECT
    m.repo_id, m.author, m.params_b, m.license, m.status, m.score,
    m.total_size_bytes,
    count(f.rfilename)              AS n_files,
    group_concat(DISTINCT f.format) AS formats,
    group_concat(DISTINCT f.quant)  AS quants
FROM models m
LEFT JOIN files f USING (repo_id)
GROUP BY m.repo_id;

DROP VIEW IF EXISTS v_storage_by_drive;
CREATE VIEW v_storage_by_drive AS
SELECT
    r.drive_label,
    count(DISTINCT r.repo_id)  AS n_models,
    count(*)                   AS n_files,
    sum(f.size_bytes)          AS bytes_held
FROM replicas r
JOIN files f USING (repo_id, rfilename)
WHERE r.present
GROUP BY r.drive_label;
