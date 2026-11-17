-- 拉力曲线采集批：分包、夹持区间与样本来源留痕。
-- 应用启动时由 SQLAlchemy Base.metadata.create_all 生成同等结构；本文件供既有库升级参考。
CREATE TABLE IF NOT EXISTS acquisition_batches(
  id SERIAL PRIMARY KEY,
  code VARCHAR(50) UNIQUE NOT NULL,
  process_id INTEGER NOT NULL REFERENCES processes(id),
  lot VARCHAR(60) NOT NULL,
  expected_codes JSON NOT NULL,
  issues JSON NOT NULL DEFAULT '[]',
  status VARCHAR(20) NOT NULL DEFAULT 'collecting',
  status_reason TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  declared_total INTEGER,
  sealed_at VARCHAR(40),
  published_at VARCHAR(40),
  archive_path TEXT NOT NULL DEFAULT '',
  archive_sha256 VARCHAR(64) NOT NULL DEFAULT '',
  created_by VARCHAR(120) NOT NULL DEFAULT '',
  created_at VARCHAR(40) NOT NULL
);
CREATE TABLE IF NOT EXISTS acquisition_packets(
  id SERIAL PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES acquisition_batches(id),
  seq INTEGER NOT NULL,
  declared_total INTEGER,
  curve JSON NOT NULL,
  events JSON NOT NULL,
  checksum VARCHAR(64) NOT NULL,
  received_by VARCHAR(120) NOT NULL DEFAULT '',
  received_at VARCHAR(40) NOT NULL,
  CONSTRAINT ux_acquisition_packets_batch_seq UNIQUE (batch_id, seq)
);
CREATE TABLE IF NOT EXISTS acquisition_attempts(
  id SERIAL PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES acquisition_batches(id),
  specimen_code VARCHAR(50) NOT NULL,
  attempt_no INTEGER NOT NULL,
  attempt_index INTEGER NOT NULL,
  open_t FLOAT NOT NULL,
  close_t FLOAT NOT NULL,
  point_count INTEGER NOT NULL,
  max_force FLOAT,
  peak_t FLOAT,
  valid BOOLEAN NOT NULL DEFAULT FALSE,
  selected BOOLEAN NOT NULL DEFAULT FALSE,
  selection_mode VARCHAR(20) NOT NULL DEFAULT 'auto',
  selection_note TEXT NOT NULL DEFAULT '',
  curve JSON NOT NULL,
  created_at VARCHAR(40) NOT NULL,
  CONSTRAINT ux_acquisition_attempts_batch_no UNIQUE (batch_id, attempt_no)
);
ALTER TABLE samples ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'manual';
ALTER TABLE samples ADD COLUMN IF NOT EXISTS acquisition_batch_id INTEGER REFERENCES acquisition_batches(id);
ALTER TABLE samples ADD COLUMN IF NOT EXISTS acquisition_attempt_id INTEGER UNIQUE REFERENCES acquisition_attempts(id);
ALTER TABLE samples ADD COLUMN IF NOT EXISTS curve_ref TEXT NOT NULL DEFAULT '';
