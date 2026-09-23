// Package store owns messages.db — the message/chat/contact archive.
//
// The sidecar is the ONLY writer. The Python gateway opens the same file
// read-only (SQLite mode=ro); WAL mode below is what makes that safe.
package store

import (
	"database/sql"
	"fmt"
)

// Store wraps the messages database. Safe for concurrent use: the pool is
// capped at one connection, so writes are serialized at the driver level.
type Store struct {
	db *sql.DB
}

const schema = `
CREATE TABLE IF NOT EXISTS chats (
    jid             TEXT PRIMARY KEY,   -- e.g. 972...@s.whatsapp.net or ...@g.us
    name            TEXT NOT NULL DEFAULT '',
    is_group        INTEGER NOT NULL DEFAULT 0,
    last_message_ts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contacts (
    jid           TEXT PRIMARY KEY,
    push_name     TEXT NOT NULL DEFAULT '',
    full_name     TEXT NOT NULL DEFAULT '',
    business_name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    chat_jid   TEXT NOT NULL,
    id         TEXT NOT NULL,
    sender_jid TEXT NOT NULL,
    ts         INTEGER NOT NULL,          -- unix seconds
    is_from_me INTEGER NOT NULL,
    kind       TEXT NOT NULL,             -- text|image|video|audio|document|sticker|reaction
    text       TEXT NOT NULL DEFAULT '',  -- body or media caption
    media_ref  TEXT,                      -- JSON MediaRef, NULL for non-media
    PRIMARY KEY (chat_jid, id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_jid, ts);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
`

// Open opens (creating if needed) the archive at path and applies the schema.
func Open(path string) (*Store, error) {
	db, err := sql.Open("sqlite3", "file:"+path)
	if err != nil {
		return nil, fmt.Errorf("open messages db: %w", err)
	}
	db.SetMaxOpenConns(1)
	// WAL lets the gateway read while we write; busy_timeout papers over
	// the brief lock during checkpoints.
	for _, pragma := range []string{
		"PRAGMA journal_mode=WAL",
		"PRAGMA busy_timeout=10000",
		"PRAGMA synchronous=NORMAL",
	} {
		if _, err := db.Exec(pragma); err != nil {
			db.Close()
			return nil, fmt.Errorf("%s: %w", pragma, err)
		}
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("apply schema: %w", err)
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }
