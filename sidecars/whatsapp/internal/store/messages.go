package store

import (
	"database/sql"
	"encoding/json"
	"fmt"
)

// MediaRef is everything whatsmeow needs to re-download and decrypt a media
// file later. Stored as JSON in messages.media_ref; we do not store the bytes.
type MediaRef struct {
	MediaType     string `json:"media_type"` // image|video|audio|document|sticker
	MimeType      string `json:"mime_type"`
	FileName      string `json:"file_name,omitempty"`
	DirectPath    string `json:"direct_path"`
	MediaKey      []byte `json:"media_key"`
	FileSHA256    []byte `json:"file_sha256"`
	FileEncSHA256 []byte `json:"file_enc_sha256"`
	FileLength    uint64 `json:"file_length"`
}

// Message is one row of the messages table.
type Message struct {
	ChatJID   string
	ID        string
	SenderJID string
	Ts        int64
	IsFromMe  bool
	Kind      string
	Text      string
	MediaRef  *MediaRef
}

// InsertMessage archives a message. Idempotent via DO NOTHING — history sync
// replays live messages, and first-write-wins also means a malicious sender
// cannot rewrite what the archive already recorded by reusing a message ID.
func (s *Store) InsertMessage(m Message) error {
	var mediaJSON any // nil → SQL NULL
	if m.MediaRef != nil {
		b, err := json.Marshal(m.MediaRef)
		if err != nil {
			return fmt.Errorf("marshal media ref: %w", err)
		}
		mediaJSON = string(b)
	}
	_, err := s.db.Exec(`
		INSERT INTO messages (chat_jid, id, sender_jid, ts, is_from_me, kind, text, media_ref)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(chat_jid, id) DO NOTHING`,
		m.ChatJID, m.ID, m.SenderJID, m.Ts, boolToInt(m.IsFromMe), m.Kind, m.Text, mediaJSON)
	return err
}

// UpsertChat records a chat, never downgrading: an empty name won't erase a
// known one, and last_message_ts only moves forward.
func (s *Store) UpsertChat(jid, name string, isGroup bool, lastMessageTs int64) error {
	_, err := s.db.Exec(`
		INSERT INTO chats (jid, name, is_group, last_message_ts) VALUES (?, ?, ?, ?)
		ON CONFLICT(jid) DO UPDATE SET
			name = CASE WHEN excluded.name != '' THEN excluded.name ELSE chats.name END,
			is_group = excluded.is_group,
			last_message_ts = MAX(chats.last_message_ts, excluded.last_message_ts)`,
		jid, name, boolToInt(isGroup), lastMessageTs)
	return err
}

// UpsertContact records a contact, keeping existing non-empty fields.
func (s *Store) UpsertContact(jid, pushName, fullName, businessName string) error {
	_, err := s.db.Exec(`
		INSERT INTO contacts (jid, push_name, full_name, business_name) VALUES (?, ?, ?, ?)
		ON CONFLICT(jid) DO UPDATE SET
			push_name     = CASE WHEN excluded.push_name     != '' THEN excluded.push_name     ELSE contacts.push_name     END,
			full_name     = CASE WHEN excluded.full_name     != '' THEN excluded.full_name     ELSE contacts.full_name     END,
			business_name = CASE WHEN excluded.business_name != '' THEN excluded.business_name ELSE contacts.business_name END`,
		jid, pushName, fullName, businessName)
	return err
}

// GetMediaRef returns the stored MediaRef for a message, or nil if the
// message has no media (or doesn't exist → sql.ErrNoRows).
func (s *Store) GetMediaRef(chatJID, messageID string) (*MediaRef, error) {
	var raw sql.NullString
	err := s.db.QueryRow(
		`SELECT media_ref FROM messages WHERE chat_jid = ? AND id = ?`,
		chatJID, messageID).Scan(&raw)
	if err != nil {
		return nil, err
	}
	if !raw.Valid {
		return nil, nil
	}
	var ref MediaRef
	if err := json.Unmarshal([]byte(raw.String), &ref); err != nil {
		return nil, fmt.Errorf("corrupt media_ref: %w", err)
	}
	return &ref, nil
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
