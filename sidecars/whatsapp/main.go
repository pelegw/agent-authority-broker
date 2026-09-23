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
	cfg, err := config.FromEnv()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	st, err := store.Open(cfg.DataDir + "/messages.db")
	if err != nil {
		log.Fatalf("store: %v", err)
	}
	defer st.Close()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	client, err := wa.New(ctx, cfg.DataDir, cfg.DeviceName, st)
	if err != nil {
		log.Fatalf("whatsapp client: %v", err)
	}

	// Start the API before connecting so /qr is reachable during first login.
	srv := &http.Server{Addr: cfg.ListenAddr, Handler: api.NewHandler(cfg.InternalToken, client)}
	go func() {
		log.Printf("internal API listening on %s", cfg.ListenAddr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http: %v", err)
		}
	}()

	// Blocks through the QR flow on first run; exits non-zero on QR expiry so
	// Docker's restart policy fetches a fresh batch of codes.
	if err := client.Run(ctx); err != nil {
		log.Fatalf("whatsapp: %v", err)
	}

	<-ctx.Done() // wait for SIGINT/SIGTERM
	log.Println("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
	client.WM.Disconnect()
	os.Exit(0)
}
