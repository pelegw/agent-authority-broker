// Package config reads the sidecar's configuration from environment variables.
package config

import (
	"fmt"
	"os"
)

type Config struct {
	// DataDir holds session.db (WhatsApp credentials) and messages.db (history).
	DataDir string
	// ListenAddr is the internal HTTP API address. Never publish this port.
	ListenAddr string
	// InternalToken must be presented by the gateway on every API request.
	InternalToken string
	// DeviceName is shown in WhatsApp > Linked devices (set at pairing time).
	DeviceName string
}

func FromEnv() (Config, error) {
	c := Config{
		DataDir:       getenv("DATA_DIR", "/data"),
		ListenAddr:    getenv("LISTEN_ADDR", ":8081"),
		InternalToken: os.Getenv("SIDECAR_TOKEN"),
		DeviceName:    getenv("DEVICE_NAME", "AAB"),
	}
	if c.InternalToken == "" {
		return c, fmt.Errorf("SIDECAR_TOKEN is required (shared secret with the gateway)")
	}
	return c, nil
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
