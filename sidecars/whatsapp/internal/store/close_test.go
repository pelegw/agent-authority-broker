package store

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

// plugin-whatsapp mounts wa_data read-only, so it can open this WAL-mode
// archive only while messages.db-wal and -shm exist (it cannot create them).
// A clean Close must fold the WAL into messages.db and remove both files:
// that is what makes archive reads answer a clean 503 while the sidecar is
// stopped, rather than reading through files a dead writer left behind
// (docs/plugins/whatsapp.md, "Read-only WAL archive"). main.go relies on this
// by closing the store on SIGTERM.
func TestCloseCheckpointsAndRemovesWALFiles(t *testing.T) {
	path := filepath.Join(t.TempDir(), "messages.db")
	s, err := Open(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	const marker = "close-checkpoint-marker"
	must(t, s.InsertMessage(Message{ChatJID: "c", ID: "m", SenderJID: "s", Ts: 1, Kind: "text", Text: marker}))

	for _, suffix := range []string{"-wal", "-shm"} {
		if _, err := os.Stat(path + suffix); err != nil {
			t.Fatalf("while open, messages.db%s should exist: %v", suffix, err)
		}
	}

	if err := s.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	for _, suffix := range []string{"-wal", "-shm"} {
		if _, err := os.Stat(path + suffix); !os.IsNotExist(err) {
			t.Errorf("after Close, messages.db%s should be gone (stat err=%v)", suffix, err)
		}
	}
	// The committed row now lives in the main file itself (SQLite stores TEXT
	// verbatim in the page), so nothing was lost with the WAL.
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(data, []byte(marker)) {
		t.Error("the committed row was not checkpointed into messages.db")
	}
}
