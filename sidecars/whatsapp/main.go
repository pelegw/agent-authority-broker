// The sidecar: logs into WhatsApp as a linked device (whatsmeow), archives
// messages into /data/messages.db, and serves a tiny token-guarded HTTP API
// for the whatsapp plugin. It holds no policy — it just speaks WhatsApp.
package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"aab/sidecars/whatsapp/internal/api"
	"aab/sidecars/whatsapp/internal/config"
	"aab/sidecars/whatsapp/internal/store"
	"aab/sidecars/whatsapp/internal/wa"
)

func main() {
	os.Exit(run())
}

// run is the whole sidecar; it returns the exit code instead of exiting.
//
// Why: os.Exit and log.Fatal skip deferred calls, and the defers below are
// what close messages.db and session.db. A closed archive has its WAL folded
// into messages.db and its -wal/-shm files removed. plugin-whatsapp mounts
// wa_data read-only and cannot create those files, so after a clean stop its
// archive reads answer 503 instead of reading through files a dead writer
// left behind (docs/plugins/whatsapp.md, "Read-only WAL archive").
func run() int {
	cfg, err := config.FromEnv()
	if err != nil {
		log.Printf("config: %v", err)
		return 1
	}

	st, err := store.Open(cfg.DataDir + "/messages.db")
	if err != nil {
		log.Printf("store: %v", err)
		return 1
	}
	defer closeLogged("messages.db", st.Close)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	client, err := wa.New(ctx, cfg.DataDir, cfg.DeviceName, st)
	if err != nil {
		log.Printf("whatsapp client: %v", err)
		return 1
	}
	// Deferred after the archive's close, so it runs first: session.db, then
	// messages.db.
	defer closeLogged("session.db", client.Close)

	// Start the API before connecting so /qr is reachable during first login.
	srv := &http.Server{Addr: cfg.ListenAddr, Handler: api.NewHandler(cfg.InternalToken, client)}
	go func() {
		log.Printf("internal API listening on %s", cfg.ListenAddr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			// No listener means no plugin can reach us: die at once (the
			// archive stays consistent; SQLite recovers the WAL on next open).
			log.Fatalf("http: %v", err)
		}
	}()

	// Blocks through the QR flow on first run; exits non-zero on QR expiry so
	// Docker's restart policy fetches a fresh batch of codes.
	if err := client.Run(ctx); err != nil {
		log.Printf("whatsapp: %v", err)
		client.WM.Disconnect()
		return 1
	}

	<-ctx.Done() // wait for SIGINT/SIGTERM
	log.Println("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx) // no new sends or reads through the API
	client.WM.Disconnect()        // no new events, so no new archive writes
	return 0
}

// closeLogged runs a deferred Close and logs a failure; at shutdown there is
// nothing better to do with it, but it must not pass silently.
func closeLogged(name string, closeFn func() error) {
	if err := closeFn(); err != nil {
		log.Printf("close %s: %v", name, err)
	}
}
