package store

import (
	"database/sql"
	"errors"
	"path/filepath"
	"testing"
)

func openTestStore(t *testing.T) *Store {
	t.Helper()
	s, err := Open(filepath.Join(t.TempDir(), "messages.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestInsertMessageIsIdempotent(t *testing.T) {
	s := openTestStore(t)
	m := Message{ChatJID: "111@s.whatsapp.net", ID: "MSG1", SenderJID: "111@s.whatsapp.net",
		Ts: 1000, Kind: "text", Text: "hello"}
	if err := s.InsertMessage(m); err != nil {
		t.Fatalf("first insert: %v", err)
	}
	// History sync replays live messages: same PK must not error, and the
	// first write wins — ID reuse must not rewrite the archive.
	m.Text = "attacker rewrite attempt"
	if err := s.InsertMessage(m); err != nil {
		t.Fatalf("replay insert: %v", err)
	}
	var text string
	if err := s.db.QueryRow(`SELECT text FROM messages WHERE id = ?`, "MSG1").Scan(&text); err != nil {
		t.Fatal(err)
	}
	if text != "hello" {
		t.Errorf("first write must win; got %q", text)
	}
}

func TestUpsertChatNeverDowngrades(t *testing.T) {
	s := openTestStore(t)
	must(t, s.UpsertChat("g@g.us", "Family", true, 2000))
	// A later event with no name and an older timestamp must not erase state.
	must(t, s.UpsertChat("g@g.us", "", true, 1000))
	var name string
	var ts int64
	if err := s.db.QueryRow(`SELECT name, last_message_ts FROM chats WHERE jid = ?`, "g@g.us").Scan(&name, &ts); err != nil {
		t.Fatal(err)
	}
	if name != "Family" || ts != 2000 {
		t.Errorf("got name=%q ts=%d, want Family/2000", name, ts)
	}
}

func TestUpsertContactKeepsNonEmptyFields(t *testing.T) {
	s := openTestStore(t)
	must(t, s.UpsertContact("111@s.whatsapp.net", "Dana", "", ""))
	must(t, s.UpsertContact("111@s.whatsapp.net", "", "Dana Levi", ""))
	var push, full string
	if err := s.db.QueryRow(`SELECT push_name, full_name FROM contacts WHERE jid = ?`, "111@s.whatsapp.net").Scan(&push, &full); err != nil {
		t.Fatal(err)
	}
	if push != "Dana" || full != "Dana Levi" {
		t.Errorf("got push=%q full=%q", push, full)
	}
}

func TestMediaRefRoundtrip(t *testing.T) {
	s := openTestStore(t)
	ref := &MediaRef{MediaType: "image", MimeType: "image/jpeg", DirectPath: "/v/x",
		MediaKey: []byte{1, 2}, FileSHA256: []byte{3}, FileEncSHA256: []byte{4}, FileLength: 99}
	must(t, s.InsertMessage(Message{ChatJID: "c", ID: "m", SenderJID: "s", Ts: 1, Kind: "image", MediaRef: ref}))

	got, err := s.GetMediaRef("c", "m")
	if err != nil {
		t.Fatal(err)
	}
	if got.DirectPath != "/v/x" || got.FileLength != 99 || len(got.MediaKey) != 2 {
		t.Errorf("roundtrip mismatch: %+v", got)
	}

	// Text message has a nil ref and no error.
	must(t, s.InsertMessage(Message{ChatJID: "c", ID: "t", SenderJID: "s", Ts: 2, Kind: "text", Text: "hi"}))
	if got, err := s.GetMediaRef("c", "t"); err != nil || got != nil {
		t.Errorf("text message: ref=%v err=%v, want nil/nil", got, err)
	}

	// Unknown message: sql.ErrNoRows.
	if _, err := s.GetMediaRef("c", "nope"); !errors.Is(err, sql.ErrNoRows) {
		t.Errorf("missing message: err=%v, want ErrNoRows", err)
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
