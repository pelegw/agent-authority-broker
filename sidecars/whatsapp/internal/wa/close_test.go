package wa

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"aab/sidecars/whatsapp/internal/store"
)

// Close must release whatsmeow's session store, so a clean shutdown leaves
// session.db checkpointed with no -wal/-shm beside it (main.go defers it).
// New opens the store and prepares a client without touching the network.
func TestCloseReleasesSessionStore(t *testing.T) {
	dir := t.TempDir()
	st, err := store.Open(filepath.Join(dir, "messages.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })

	c, err := New(context.Background(), dir, "", st)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	wal := filepath.Join(dir, "session.db-wal")
	if _, err := os.Stat(wal); err != nil {
		t.Fatalf("while open, session.db-wal should exist: %v", err)
	}
	if err := c.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if _, err := os.Stat(wal); !os.IsNotExist(err) {
		t.Errorf("after Close, session.db-wal should be gone (stat err=%v)", err)
	}
}

func TestCloseWithoutSessionStoreIsNoop(t *testing.T) {
	if err := (&Client{}).Close(); err != nil {
		t.Fatalf("Close on a client without a store: %v", err)
	}
}
