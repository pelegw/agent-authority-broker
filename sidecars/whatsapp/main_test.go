package main

import "testing"

// run must hand back an exit code rather than exit: main's deferred Close
// calls (the archive's final checkpoint) only run when it returns. A
// log.Fatal or os.Exit on this path would kill the test binary instead.
func TestRunReturnsExitCodeOnConfigError(t *testing.T) {
	t.Setenv("SIDECAR_TOKEN", "")
	if code := run(); code != 1 {
		t.Fatalf("run() = %d, want 1 for a missing SIDECAR_TOKEN", code)
	}
}
