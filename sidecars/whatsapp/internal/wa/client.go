// Package wa wraps whatsmeow: session lifecycle, QR login, event ingestion
// into the store, and the handful of actions the internal API exposes.
package wa

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"sync"
	"time"

	"github.com/mdp/qrterminal/v3"
	"go.mau.fi/whatsmeow"
	waCompanionReg "go.mau.fi/whatsmeow/proto/waCompanionReg"
	wmstore "go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"

	"aab/sidecars/whatsapp/internal/store"
)

// LogLevel is whatsmeow's minimum log level (DEBUG, INFO, WARN, ERROR),
// set by main from LOG_LEVEL before New is called.
var LogLevel = "INFO"

type Client struct {
	WM *whatsmeow.Client
	st *store.Store
	// container is whatsmeow's session store (session.db); Close releases it.
	container *sqlstore.Container

	mu     sync.RWMutex
	qrCode string // current pairing code while waiting for a scan, else ""
	fatal  string // non-empty once a non-recoverable account state is seen
}

func (c *Client) setFatal(reason string) {
	c.mu.Lock()
	c.fatal = reason
	c.mu.Unlock()
}

func (c *Client) fatalReason() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.fatal
}

// New opens the whatsmeow session store (sessionDir/session.db holds the
// account credentials — guard that file) and prepares a client. Call Run to
// connect. sessionDir is its own volume in compose, mounted by the sidecar
// alone; the archive (st) lives elsewhere, in the volume plugin-whatsapp
// reads.
//
// deviceName is what shows up under WhatsApp > Linked devices. It is sent
// during pairing, so changing it only takes effect on the next (re-)link.
func New(ctx context.Context, sessionDir, deviceName string, st *store.Store) (*Client, error) {
	// Present a friendly name + a known platform type; otherwise WhatsApp
	// labels the linked device "Other Device".
	if deviceName != "" {
		wmstore.DeviceProps.Os = proto.String(deviceName)
	}
	wmstore.DeviceProps.PlatformType = waCompanionReg.DeviceProps_CHROME.Enum()

	dsn := "file:" + sessionDir + "/session.db?_pragma=foreign_keys(1)&_pragma=journal_mode(WAL)&_pragma=busy_timeout(10000)"
	// Colour off: these lines end up in log files and log shippers.
	container, err := sqlstore.New(ctx, "sqlite3", dsn, waLog.Stdout("SessionDB", "WARN", false))
	if err != nil {
		return nil, fmt.Errorf("open session store: %w", err)
	}
	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		return nil, fmt.Errorf("get device: %w", err)
	}
	c := &Client{
		WM:        whatsmeow.NewClient(device, waLog.Stdout("WhatsApp", LogLevel, false)),
		st:        st,
		container: container,
	}
	c.WM.AddEventHandler(c.handleEvent)
	return c, nil
}

// Close releases the session store (session.db) so SQLite can checkpoint its
// WAL on the way out. Shutdown only, after WM.Disconnect: nothing may use the
// client afterwards. The archive store is closed separately by its owner.
func (c *Client) Close() error {
	if c.container == nil {
		return nil
	}
	return c.container.Close()
}

// Run connects to WhatsApp. If the device isn't paired yet it drives the QR
// flow: codes are printed to the container log AND kept available for the
// /qr PNG endpoint. Blocks until pairing completes; returns an error when the
// batch of QR codes expires — exit and let Docker restart us for fresh codes.
func (c *Client) Run(ctx context.Context) error {
	if c.WM.Store.ID != nil {
		log.Printf("whatsapp session found; connecting")
		return c.WM.Connect() // already paired; whatsmeow reconnects on drops
	}
	log.Printf("whatsapp not paired; waiting for a QR scan")

	qrChan, err := c.WM.GetQRChannel(ctx)
	if err != nil {
		return fmt.Errorf("qr channel: %w", err)
	}
	if err := c.WM.Connect(); err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	paired := false
	for item := range qrChan {
		switch item.Event {
		case "code":
			c.setQR(item.Code)
			// The event only; the code itself is printed below as the QR
			// block the operator scans, never as a log value.
			log.Printf("qr event=code")
			fmt.Println("\n==== Scan this QR with WhatsApp (Settings > Linked devices) ====")
			qrterminal.GenerateHalfBlock(item.Code, qrterminal.L, os.Stdout)
			fmt.Println("(also available as PNG via the broker: GET /v1/admin/plugins/whatsapp/connect/qr.png)")
		case "success":
			// Track success explicitly. whatsmeow closes this channel the
			// instant it emits "success", but IsLoggedIn() only flips true a
			// few seconds later — after the server forces a post-pair
			// disconnect+reconnect. Consulting IsLoggedIn() right here would
			// therefore misread every successful scan as expiry.
			paired = true
			c.setQR("")
			log.Printf("qr event=success")
			fmt.Println("==== WhatsApp login successful ====")
		default:
			// "timeout" (codes expired) and "err-*" land here.
			log.Printf("qr event=%s", strconvQuoteIfNeeded(item.Event))
		}
	}
	c.setQR("")

	if ctx.Err() != nil {
		return nil // clean shutdown during QR wait, not a failure
	}
	if !paired && !c.WM.IsLoggedIn() {
		return errors.New("QR codes expired before being scanned; restart the sidecar to get new ones")
	}
	// Paired: wait for the post-pair reconnect to actually establish before we
	// report success, so we never return nil on a dead connection (whatsmeow
	// makes only a single 515 reconnect attempt and merely logs if it fails).
	return c.waitConnected(ctx, 45*time.Second)
}

// waitConnected blocks until the client is logged in and connected, or the
// timeout/ctx elapses. On timeout it returns an error so main exits non-zero
// and Docker restarts us — the saved session then reconnects cleanly.
func (c *Client) waitConnected(ctx context.Context, timeout time.Duration) error {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		if c.WM.IsLoggedIn() && c.WM.IsConnected() {
			return nil
		}
		select {
		case <-ctx.Done():
			return nil
		case <-deadline.C:
			return errors.New("paired but connection did not establish; restarting to reconnect")
		case <-ticker.C:
		}
	}
}

// strconvQuoteIfNeeded keeps an event name from whatsmeow on one line.
func strconvQuoteIfNeeded(s string) string {
	for _, r := range s {
		if r <= ' ' || r == '"' || r == '=' || r == '\\' || r > '~' {
			return fmt.Sprintf("%q", s)
		}
	}
	return s
}

func (c *Client) setQR(code string) {
	c.mu.Lock()
	c.qrCode = code
	c.mu.Unlock()
}

func (c *Client) currentQR() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.qrCode
}
