-- aisha schema v2 — temporal validity for the knowledge triple store.
--
-- What changes
--   * `knowledge` gains `valid_from` (time the assertion starts holding) and
--     `valid_to` (time it stops; NULL = still current).
--   * The old UNIQUE(subject, predicate, object) is dropped — the same triple
--     may now recur across disjoint validity windows (e.g. "alice lives_in
--     Paris" 2023→2025 and "alice lives_in Abu Dhabi" 2026→NULL).
--   * A partial UNIQUE index keeps the upsert-on-re-assert behaviour for the
--     common case: at most one currently-open row per (s, p, o).
--
-- Backfill: every existing row becomes "open" — `valid_from = ts`, `valid_to = NULL`.
-- That's faithful to the pre-migration meaning (each row was "current truth").

CREATE TABLE knowledge_new (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  subject     TEXT NOT NULL,
  predicate   TEXT NOT NULL,
  object      TEXT NOT NULL,
  confidence  REAL NOT NULL DEFAULT 1.0,
  source      TEXT,
  ts          REAL NOT NULL,
  valid_from  REAL NOT NULL,
  valid_to    REAL
);

INSERT INTO knowledge_new (id, subject, predicate, object, confidence, source, ts, valid_from, valid_to)
  SELECT id, subject, predicate, object, confidence, source, ts, ts, NULL FROM knowledge;

DROP TABLE knowledge;
ALTER TABLE knowledge_new RENAME TO knowledge;

CREATE INDEX idx_kn_s        ON knowledge(subject);
CREATE INDEX idx_kn_p        ON knowledge(predicate);
CREATE INDEX idx_kn_o        ON knowledge(object);
CREATE INDEX idx_kn_validity ON knowledge(valid_from, valid_to);

-- At most one "currently open" row per (subject, predicate, object). The ON
-- CONFLICT clause in knowledge_add targets this index to preserve the
-- idempotent-upsert behaviour for re-asserted facts.
CREATE UNIQUE INDEX idx_kn_open_spo
  ON knowledge(subject, predicate, object)
  WHERE valid_to IS NULL;
