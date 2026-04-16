-- aisha schema v3 — annotate conversation rows with the tool-set
-- fingerprint that was live when they were written.
--
-- Why: the model anchors on prior tool-use recipes in its own response text.
-- When the tool set changes (new tool added, existing one's description
-- edited), earlier turns describe workflows that are no longer optimal. We
-- never delete those rows (see the soft-delete principle), but at
-- context-assembly time we want to know which turns pre-date the current
-- tool set so we can annotate them for the model instead of letting them
-- silently anchor its decisions.
--
-- The column is nullable: historical rows get NULL and are treated as
-- pre-fingerprint (i.e. older generation) by the context filter.

ALTER TABLE conversations ADD COLUMN tool_fingerprint TEXT;

CREATE INDEX IF NOT EXISTS idx_conv_fingerprint ON conversations(tool_fingerprint);
