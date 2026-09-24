package main

import (
	"os"
	"path/filepath"
	"testing"

	"aab/sidecars/whatsapp/internal/config"
)

// run must hand back an exit code rather than exit: main's deferred Close
// calls (the archive's final checkpoint) only run when it returns. A
// log.Fatal or os.Exit on this path would kill the test binary instead.
func TestRunReturnsExitCodeOnConfigError(t *testing.T) {
	t.Setenv("SIDECAR_TOKEN", "")
	if code := run(); code != 1 {
		t.Fatalf("run() = %d, want 1 for a missing SIDECAR_TOKEN", code)
	}
}

// A session directory that does not exist yet is created private to the
// sidecar: session.db is the WhatsApp credential.
func TestPrepareSessionDirCreatesItPrivate(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "session")
	if err := prepareSessionDir(config.Config{SessionDir: dir, DataDir: t.TempDir()}); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if !info.IsDir() || info.Mode().Perm() != 0o700 {
		t.Errorf("session dir mode %v, want a 0700 directory", info.Mode())
	}
}

// A session directory that cannot be created is an exit code, not a crash.
func TestRunReturnsExitCodeWhenSessionDirCannotBeCreated(t *testing.T) {
	blocker := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SIDECAR_TOKEN", "t")
	t.Setenv("SESSION_DIR", filepath.Join(blocker, "session")) // under a regular file
	t.Setenv("DATA_DIR", t.TempDir())
	if code := run(); code != 1 {
		t.Fatalf("run() = %d, want 1 when the session dir cannot be created", code)
	}
}
