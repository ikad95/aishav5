-- aisha schema v1
-- Runtime pragmas are set in store.py; this file is schema-only.

CREATE TABLE IF NOT EXISTS conversations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  source      TEXT NOT NULL,                -- 'terminal', 'slack:C...', etc.
  user_id     TEXT,
  ts          REAL NOT NULL,
  role        TEXT NOT NULL,                -- user/assistant/system/tool/error
  content     TEXT NOT NULL,
  meta        TEXT                          -- JSON
);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_conv_source  ON conversations(source, ts);
CREATE INDEX IF NOT EXISTS idx_conv_user    ON conversations(user_id, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
  content,
  session_id UNINDEXED,
  source     UNINDEXED,
  user_id    UNINDEXED,
  content='conversations',
  content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS conv_ai AFTER INSERT ON conversations BEGIN
  INSERT INTO conversations_fts(rowid, content, session_id, source, user_id)
  VALUES (new.id, new.content, new.session_id, new.source, new.user_id);
END;
CREATE TRIGGER IF NOT EXISTS conv_ad AFTER DELETE ON conversations BEGIN
  INSERT INTO conversations_fts(conversations_fts, rowid, content, session_id, source, user_id)
  VALUES ('delete', old.id, old.content, old.session_id, old.source, old.user_id);
END;
CREATE TRIGGER IF NOT EXISTS conv_au AFTER UPDATE ON conversations BEGIN
  INSERT INTO conversations_fts(conversations_fts, rowid, content, session_id, source, user_id)
  VALUES ('delete', old.id, old.content, old.session_id, old.source, old.user_id);
  INSERT INTO conversations_fts(rowid, content, session_id, source, user_id)
  VALUES (new.id, new.content, new.session_id, new.source, new.user_id);
END;

CREATE TABLE IF NOT EXISTS knowledge (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  subject    TEXT NOT NULL,
  predicate  TEXT NOT NULL,
  object     TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  source     TEXT,
  ts         REAL NOT NULL,
  UNIQUE(subject, predicate, object)
);
CREATE INDEX IF NOT EXISTS idx_kn_s ON knowledge(subject);
CREATE INDEX IF NOT EXISTS idx_kn_p ON knowledge(predicate);
CREATE INDEX IF NOT EXISTS idx_kn_o ON knowledge(object);

CREATE TABLE IF NOT EXISTS entities (
  name       TEXT PRIMARY KEY,
  type       TEXT NOT NULL,
  properties TEXT,                          -- JSON
  ts         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  user_id    TEXT PRIMARY KEY,
  profile    TEXT NOT NULL,                 -- JSON blob
  updated_at REAL NOT NULL
);

-- Catch-all key/value for small state blobs (human_model, satisfaction, etc.)
CREATE TABLE IF NOT EXISTS kv (
  namespace  TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,                 -- JSON
  updated_at REAL NOT NULL,
  PRIMARY KEY (namespace, key)
);
