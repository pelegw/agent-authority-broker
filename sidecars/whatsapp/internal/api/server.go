// Package api is the sidecar's internal HTTP surface. It is reachable only on
// the Docker-internal network and every route (except /health) requires the
// shared X-Internal-Token. No policy lives here — that's the gateway's job.
package api

import (
	"context"
	"crypto/subtle"
	"net/http"

	"aab/sidecars/whatsapp/internal/wa"
)

// WhatsApp is what the handlers need from the WA client; a narrow interface
// so tests can substitute a fake without a live session.
type WhatsApp interface {
	Status() wa.Status
	QRPNG() ([]byte, error)
	SendText(ctx context.Context, to, text string) (wa.SendResult, error)
	MediaByMessage(ctx context.Context, chatJID, messageID string) ([]byte, string, error)
}

// NewHandler builds the route table. token guards everything but /health.
func NewHandler(token string, w WhatsApp) http.Handler {
	h := &handlers{wa: w}
	auth := requireToken(token)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", h.health) // tokenless: container liveness only
	mux.Handle("GET /status", auth(h.status))
	mux.Handle("GET /qr", auth(h.qr))
	mux.Handle("POST /send", auth(h.send))
	mux.Handle("GET /media", auth(h.media))
	return mux
}

// requireToken enforces the shared secret with a constant-time compare.
func requireToken(token string) func(http.HandlerFunc) http.Handler {
	return func(next http.HandlerFunc) http.Handler {
		return http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
			got := r.Header.Get("X-Internal-Token")
			if subtle.ConstantTimeCompare([]byte(got), []byte(token)) != 1 {
				writeError(rw, http.StatusUnauthorized, "missing or invalid X-Internal-Token")
				return
			}
			next(rw, r)
		})
	}
}
