package wa

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"regexp"
	"strings"

	qrcode "github.com/skip2/go-qrcode"
	"go.mau.fi/whatsmeow"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"

	"aab/sidecars/whatsapp/internal/store"
)

// Sentinel errors the API layer maps to HTTP statuses.
var (
	ErrLoggedIn  = errors.New("already logged in — no QR available")
	ErrNoQR      = errors.New("no QR code available yet, retry shortly")
	ErrNotFound  = errors.New("message not found")
	ErrNoMedia   = errors.New("message has no media")
	ErrNotLinked = errors.New("not logged in to WhatsApp")
)

type Status struct {
	Connected    bool   `json:"connected"`
	LoggedIn     bool   `json:"logged_in"`
	JID          string `json:"jid"`
	PushName     string `json:"push_name"`
	WaitingForQR bool   `json:"waiting_for_qr"`
	Fatal        string `json:"fatal,omitempty"` // non-recoverable state, e.g. client outdated / banned
}

func (c *Client) Status() Status {
	s := Status{
		Connected:    c.WM.IsConnected(),
		LoggedIn:     c.WM.IsLoggedIn(),
		WaitingForQR: c.currentQR() != "",
		Fatal:        c.fatalReason(),
	}
	if id := c.WM.Store.ID; id != nil {
		s.JID = id.ToNonAD().String()
		s.PushName = c.WM.Store.PushName
	}
	return s
}

// QRPNG renders the current pairing code as a PNG for browser-based login.
func (c *Client) QRPNG() ([]byte, error) {
	if c.WM.IsLoggedIn() {
		return nil, ErrLoggedIn
	}
	code := c.currentQR()
	if code == "" {
		return nil, ErrNoQR
	}
	return qrcode.Encode(code, qrcode.Medium, 512)
}

type SendResult struct {
	MessageID string `json:"message_id"`
	Ts        int64  `json:"ts"`
}

// SendText sends a plain text message and archives it, so from-me messages
// show up in the same history the plugin reads.
func (c *Client) SendText(ctx context.Context, to, text string) (SendResult, error) {
	if !c.WM.IsLoggedIn() {
		return SendResult{}, ErrNotLinked
	}
	jid, err := ParseRecipient(to)
	if err != nil {
		return SendResult{}, err
	}
	resp, err := c.WM.SendMessage(ctx, jid, &waE2E.Message{Conversation: proto.String(text)})
	if err != nil {
		return SendResult{}, fmt.Errorf("send: %w", err)
	}
	res := SendResult{MessageID: string(resp.ID), Ts: resp.Timestamp.Unix()}
	self := ""
	if id := c.WM.Store.ID; id != nil {
		self = id.ToNonAD().String()
	}
	_ = c.st.InsertMessage(store.Message{
		ChatJID: jid.String(), ID: res.MessageID, SenderJID: self,
		Ts: res.Ts, IsFromMe: true, Kind: "text", Text: text,
	})
	_ = c.st.UpsertChat(jid.String(), "", jid.Server == types.GroupServer, res.Ts)
	return res, nil
}

// MediaByMessage re-downloads and decrypts the media of an archived message.
func (c *Client) MediaByMessage(ctx context.Context, chatJID, messageID string) ([]byte, string, error) {
	ref, err := c.st.GetMediaRef(chatJID, messageID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, "", ErrNotFound
	}
	if err != nil {
		return nil, "", err
	}
	if ref == nil {
		return nil, "", ErrNoMedia
	}
	data, err := c.WM.DownloadMediaWithPath(ctx,
		ref.DirectPath, ref.FileEncSHA256, ref.FileSHA256, ref.MediaKey,
		mediaTypeOf(ref.MediaType), "", false)
	if err != nil {
		return nil, "", fmt.Errorf("download media: %w", err)
	}
	return data, ref.MimeType, nil
}

func mediaTypeOf(s string) whatsmeow.MediaType {
	switch s {
	case "image", "sticker":
		return whatsmeow.MediaImage
	case "video":
		return whatsmeow.MediaVideo
	case "audio":
		return whatsmeow.MediaAudio
	default:
		return whatsmeow.MediaDocument
	}
}

var phoneRe = regexp.MustCompile(`^\+?[0-9]{6,20}$`)

// ParseRecipient accepts a full JID ("...@s.whatsapp.net", "...@g.us") or a
// bare international phone number ("+972501234567"). Pure and unit-tested.
func ParseRecipient(to string) (types.JID, error) {
	to = strings.TrimSpace(to)
	if strings.Contains(to, "@") {
		jid, err := types.ParseJID(to)
		if err != nil {
			return types.EmptyJID, fmt.Errorf("invalid JID %q: %w", to, err)
		}
		if jid.User == "" {
			return types.EmptyJID, fmt.Errorf("recipient %q has no user part", to)
		}
		// Accept @lid too: modern WhatsApp hands out hidden-user JIDs for many
		// chats, and the archive stores them — they must be reply-able.
		switch jid.Server {
		case types.DefaultUserServer, types.GroupServer, types.HiddenUserServer:
		default:
			return types.EmptyJID, fmt.Errorf("unsupported JID server %q (want user, group, or lid)", jid.Server)
		}
		// Strip device/agent suffixes ("...:12@s.whatsapp.net") so what we send
		// to is exactly the canonical JID the broker's grant check compared.
		return jid.ToNonAD(), nil
	}
	if !phoneRe.MatchString(to) {
		return types.EmptyJID, fmt.Errorf("recipient %q is neither a JID nor an international phone number", to)
	}
	return types.NewJID(strings.TrimPrefix(to, "+"), types.DefaultUserServer), nil
}
