-- ModelArk catalog schema (SQLite, WAL mode). The shared core (modelark.core), reusable by
-- future modelark modules. git-annex owns *where bytes physically live*;
-- the `replicas` table mirrors `git annex whereis` for offline-queryable location.
-- WAL journaling → concurrent readers + one writer across processes (DEC-024, replacing DuckDB's
-- single-writer lock). SQLite is dynamically typed; the declared types below are affinity hints.

-- One row per Hugging Face repo we know about (downloaded or not).
CREATE TABLE IF NOT EXISTS models (
    repo_id            VARCHAR PRIMARY KEY,   -- "org/name"
    author             VARCHAR,
    model_name         VARCHAR,
    params_b           DOUBLE,                -- billions of params (NULL if undeclared)
    architecture       VARCHAR,               -- e.g. LlamaForCausalLM
    modality           VARCHAR,               -- domain: text | vision | audio | multimodal | image-gen | video-gen
    category           VARCHAR,               -- generative-llm|encoder|seq2seq|translation|embedding|reranker|classifier|qa (DEC-002)
    variant            VARCHAR,               -- base | instruct | reasoning
    pipeline_tag       VARCHAR,               -- raw HF hint (unreliable; kept for reference)
    library            VARCHAR,               -- transformers | gguf | mlx | ...
    license            VARCHAR,
    gated              VARCHAR,               -- false | auto | manual
    private            BOOLEAN,
    likes              INTEGER,
    downloads_30d      INTEGER,
    downloads_all      BIGINT,
    trending_score     DOUBLE,
    tags               TEXT,                  -- JSON array (was DuckDB VARCHAR[]; json-encoded in Python)
    total_size_bytes   BIGINT,
    hf_last_modified   TIMESTAMP,
    hf_created_at      TIMESTAMP,
    status             VARCHAR DEFAULT 'discovered',  -- discovered|wishlist|fetching|archived|verified|skip
    numcopies          INTEGER DEFAULT 1,     -- 1 | 2 (must-have: 2nd copy on the replica tier) — DEC-014
    score              DOUBLE,
    notes              VARCHAR,
    discovered_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One row per file inside a repo.
CREATE TABLE IF NOT EXISTS files (
    repo_id      VARCHAR,
    rfilename    VARCHAR,
    size_bytes   BIGINT,
    is_lfs       BOOLEAN,
    sha256       VARCHAR,                     -- LFS canonical hash (NULL for tiny git blobs)
    format       VARCHAR,                     -- safetensors|gguf|pytorch|onnx|mlx|aux|other
    quant        VARCHAR,                     -- Q4_K_M | bf16 | gptq | awq | ...
    quant_bits   DOUBLE,                      -- nominal bits-per-weight
    safety       VARCHAR,                     -- safe | pickle | unknown
    PRIMARY KEY (repo_id, rfilename)
);

-- The physical drive fleet (the "tape library").
CREATE TABLE IF NOT EXISTS drives (
    drive_label        VARCHAR PRIMARY KEY,   -- "drive-07"
    fs_uuid            VARCHAR,
    annex_uuid         VARCHAR,
    capacity_bytes     BIGINT,
    free_bytes         BIGINT,
    hw_model           VARCHAR,
    serial             VARCHAR,
    physical_location  VARCHAR,               -- "shelf box A, slot 3"
    role               VARCHAR DEFAULT 'primary',  -- primary (bin-packed) | replica (copies only) — DEC-014
    raid_backed        BOOLEAN DEFAULT false,      -- redundant tier (iSCSI/NAS RAID): must-have copy#1 home — DEC-017
    health             VARCHAR,
    last_seen          TIMESTAMP,
    notes              VARCHAR
);

-- Which file lives on which drive (mirror of `git annex whereis`).
CREATE TABLE IF NOT EXISTS replicas (
    repo_id      VARCHAR,
    rfilename    VARCHAR,
    drive_label  VARCHAR,
    annex_key    VARCHAR,
    present      BOOLEAN DEFAULT TRUE,
    verified_at  TIMESTAMP,
    added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo_id, rfilename, drive_label)
);

-- Verification results (one row per repo; re-runnable).
CREATE TABLE IF NOT EXISTS verifications (
    repo_id          VARCHAR PRIMARY KEY,
    checksum_ok      BOOLEAN,   -- all local files sha256 == HF canonical (NULL if not downloaded)
    structural_ok    BOOLEAN,   -- Tier A: headers parse, shapes/dtypes consistent
    shards_complete  BOOLEAN,   -- index references all present; no missing shards
    format_safety    VARCHAR,   -- safe | pickle-present | mixed | unknown
    pickle_scan      VARCHAR,   -- clean | flagged | n/a
    hf_scan_status   VARCHAR,   -- from HF API (NULL if unreported)
    signature        VARCHAR,   -- none | sigstore | gpg
    signer           VARCHAR,
    load_tier_max    VARCHAR,   -- 'A' or 'B' (highest tier passed)
    functional_ok    BOOLEAN,   -- Tier B result; NULL if too big / not run
    detail           VARCHAR,   -- human-readable findings
    verified_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tool_versions    VARCHAR
);

-- The download set built in the portal; this IS the wishlist the fetch pipeline reads.
CREATE TABLE IF NOT EXISTS selection (
    repo_id      VARCHAR PRIMARY KEY,
    added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finalized_at TIMESTAMP   -- NULL = still in the cart; set by "Finish" = committed wishlist
);

-- What the fetch pipeline has archived, and where (the durable record of bytes on drives).
CREATE TABLE IF NOT EXISTS archived (
    repo_id      VARCHAR,
    rfilename    VARCHAR,          -- original path within the HF repo
    stored_name  VARCHAR,          -- name on disk (".znn" when compressed)
    drive_label  VARCHAR,
    orig_sha256  VARCHAR,          -- HF canonical (= decompressed) hash
    znn_sha256   VARCHAR,          -- compressed-blob hash (NULL when stored raw)
    orig_bytes   BIGINT,
    stored_bytes BIGINT,
    compressed   BOOLEAN,
    annex_key    VARCHAR,
    verified_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo_id, rfilename, drive_label)
);

-- Per-repo fetch outcomes — feeds the Download Status view (append-only, one row per attempt).
CREATE TABLE IF NOT EXISTS fetch_events (
    repo_id      VARCHAR,
    event_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    outcome      VARCHAR,        -- archived | auth | rate_limited | waiting | error | throttled
    bytes        BIGINT,         -- downloaded bytes (archived outcome)
    wait_seconds DOUBLE,         -- backoff waited / server Retry-After (rate_limited/waiting)
    detail       VARCHAR
);

-- A first-class Plan (#33, DEF-016) — one fill campaign's identity: a FIXED set of registered
-- drives (plan_drives) + the global selection/archived it fills + a provisioning mode. The three
-- LIVE numbers (uncompressed/compressed/capacity, modelark.plan.totals) fuel the level-1
-- capacity failsafe. One plan for now (`ark`); selection/archived stay GLOBAL — a future plan_id
-- column on them is the multi-plan future (a DEF). Exactly one row has is_active (the current
-- backend/portal context); the #35 UI gate additionally forces an explicit operator pick per session.
CREATE TABLE IF NOT EXISTS plans (
    plan_id      VARCHAR PRIMARY KEY,             -- stable slug, e.g. "ark"
    name         VARCHAR,
    annex_root   VARCHAR,                         -- the git-annex map root this plan fills
    provisioning VARCHAR DEFAULT 'uncompressed',  -- 'uncompressed' (over-provision, never runs out) | 'compressed' (bet on ZipNN)
    status       VARCHAR DEFAULT 'active',        -- active | archived (lifecycle, NOT selection)
    is_active    BOOLEAN DEFAULT false,           -- the one currently-selected plan (backend context)
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes        VARCHAR
);

-- The fixed drive set a plan fills — the ONLY capacity that exists for it (the failsafe boundary).
-- Populated at bootstrap (owns every registered drive) and by registration (#34) as drives are added.
CREATE TABLE IF NOT EXISTS plan_drives (
    plan_id      VARCHAR,
    drive_label  VARCHAR,
    added_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plan_id, drive_label)
);

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
