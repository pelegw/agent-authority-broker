// Package config reads the sidecar's configuration from environment variables.
package config

import (
	"fmt"
	"os"
	"strings"
)

type Config struct {
	// DataDir holds messages.db (the archive). plugin-whatsapp mounts it
	// read-only to serve archive reads.
	DataDir string
	// SessionDir holds session.db, whatsmeow's store and the WhatsApp account
	// credential. In compose it is its own volume that only the sidecar
	// mounts, so no other container can read the session.
	SessionDir string
	// ListenAddr is the internal HTTP API address. Never publish this port.
	ListenAddr string
	// InternalToken must be presented by the plugin on every API request.
	InternalToken string
	// DeviceName is shown in WhatsApp > Linked devices (set at pairing time).
	DeviceName string
	// LogLevel is LOG_LEVEL normalized to whatsmeow's names (DEBUG, INFO,
	// WARN, ERROR); the same variable the Python services read. Anything
	// else is INFO, and LogLevelInvalid says so (main logs a warning).
	LogLevel        string
	LogLevelInvalid bool
}

// DefaultSessionDir is where session.db lives when neither SESSION_DIR nor
// DATA_DIR is set: the image's /session volume.
const DefaultSessionDir = "/session"

func FromEnv() (Config, error) {
	c := Config{
		DataDir:       getenv("DATA_DIR", "/data"),
		SessionDir:    sessionDir(),
		ListenAddr:    getenv("LISTEN_ADDR", ":8081"),
		InternalToken: os.Getenv("SIDECAR_TOKEN"),
		DeviceName:    getenv("DEVICE_NAME", "AAB"),
	}
	c.LogLevel, c.LogLevelInvalid = logLevel(os.Getenv("LOG_LEVEL"))
	if c.InternalToken == "" {
		return c, fmt.Errorf("SIDECAR_TOKEN is required (shared secret with the gateway)")
	}
	return c, nil
}

// sessionDir is SESSION_DIR; when that is unset, an explicitly set DATA_DIR
// (a dev run or a test that keeps everything in one directory, the layout
// before the session had its own volume); otherwise /session. The image sets
// SESSION_DIR=/session and compose sets it again, so a container never takes
// the DATA_DIR fallback and never puts the session where plugin-whatsapp can
// read it.
func sessionDir() string {
	if v := os.Getenv("SESSION_DIR"); v != "" {
		return v
	}
	if v := os.Getenv("DATA_DIR"); v != "" {
		return v
	}
	return DefaultSessionDir
}

// logLevel maps LOG_LEVEL (case-insensitive; WARNING and CRITICAL as the
// Python services spell them) onto whatsmeow's level names.
func logLevel(raw string) (string, bool) {
	switch strings.ToUpper(strings.TrimSpace(raw)) {
	case "":
		return "INFO", false
	case "DEBUG":
		return "DEBUG", false
	case "INFO":
		return "INFO", false
	case "WARN", "WARNING":
		return "WARN", false
	case "ERROR", "CRITICAL":
		return "ERROR", false
	}
	return "INFO", true
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
