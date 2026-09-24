package config

import "testing"

// Where session.db (the WhatsApp credential) and messages.db (the archive)
// go. In compose they are different volumes: only the sidecar mounts the
// session, plugin-whatsapp mounts the archive read-only.
func TestSessionAndDataDirs(t *testing.T) {
	cases := []struct {
		name, sessionEnv, dataEnv string
		wantSession, wantData     string
	}{
		{"defaults", "", "", "/session", "/data"},
		{"session dir set", "/s", "", "/s", "/data"},
		{"both set", "/s", "/d", "/s", "/d"},
		// A dev run or test with DATA_DIR alone keeps the single-directory
		// layout it had before the session got its own volume.
		{"data dir only", "", "/d", "/d", "/d"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("SIDECAR_TOKEN", "t")
			t.Setenv("SESSION_DIR", tc.sessionEnv)
			t.Setenv("DATA_DIR", tc.dataEnv)
			c, err := FromEnv()
			if err != nil {
				t.Fatal(err)
			}
			if c.SessionDir != tc.wantSession || c.DataDir != tc.wantData {
				t.Errorf("SessionDir=%q DataDir=%q, want %q and %q",
					c.SessionDir, c.DataDir, tc.wantSession, tc.wantData)
			}
		})
	}
}

func TestTokenRequired(t *testing.T) {
	t.Setenv("SIDECAR_TOKEN", "")
	if _, err := FromEnv(); err == nil {
		t.Fatal("FromEnv without SIDECAR_TOKEN should fail")
	}
}
