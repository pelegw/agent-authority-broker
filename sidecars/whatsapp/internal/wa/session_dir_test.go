package wa

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"aab/sidecars/whatsapp/internal/store"
)

// session.db goes to the session directory, never beside messages.db: in
// compose the archive's volume is mounted by plugin-whatsapp and the
// session's volume by nobody but the sidecar.
func TestSessionStoreOpensInSessionDirNotDataDir(t *testing.T) {
	dataDir, sessionDir := t.TempDir(), t.TempDir()
	st, err := store.Open(filepath.Join(dataDir, "messages.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })

	c, err := New(context.Background(), sessionDir, "", st)
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	t.Cleanup(func() { c.Close() })

	if _, err := os.Stat(filepath.Join(sessionDir, "session.db")); err != nil {
		t.Errorf("session.db should be in the session dir: %v", err)
	}
	matches, err := filepath.Glob(filepath.Join(dataDir, "session.db*"))
	if err != nil {
		t.Fatal(err)
	}
	if len(matches) != 0 {
		t.Errorf("no session file may be in the data dir, found %v", matches)
	}
}
