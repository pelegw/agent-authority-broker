package wa

import (
	"context"
	"fmt"
	"log"
	"os"

	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"aab/sidecars/whatsapp/internal/store"
)

// handleEvent is registered with whatsmeow and receives every account event.
func (c *Client) handleEvent(evt interface{}) {
	switch v := evt.(type) {
	case *events.Message:
		c.ingestMessage(v)
	case *events.HistorySync:
		c.ingestHistorySync(v)
	case *events.Connected:
		log.Println("connected to WhatsApp")
		go c.syncContacts()
	case *events.Disconnected:
		log.Println("disconnected (whatsmeow auto-reconnects)")
	case *events.LoggedOut:
		// The user unlinked this device (or it expired). The stored session is
		// dead; delete it and exit so Docker restarts us into a fresh QR flow.
		log.Println("logged out by WhatsApp — clearing session, restarting for re-pair")
		if err := c.WM.Store.Delete(context.Background()); err != nil {
			log.Printf("failed to clear session: %v", err)
		}
		os.Exit(1)
	case *events.StreamReplaced:
		// Another linked-device session took over this slot. whatsmeow will not
		// reconnect; exit so a restart re-establishes (or surfaces a real conflict).
		c.setFatal("stream replaced by another session")
		log.Println("stream replaced by another WhatsApp session — restarting")
		os.Exit(1)
	case *events.ClientOutdated:
		// WhatsApp rejected our client version. Auto-reconnect can't fix this;
		// record it so /status stops lying and exit for operator attention.
		c.setFatal("client version outdated — the whatsmeow dependency must be updated")
		log.Println("FATAL: WhatsApp says this client is outdated; update whatsmeow and rebuild")
		os.Exit(1)
	case *events.TemporaryBan:
		// Do NOT exit (that would crash-loop): surface it and keep serving reads.
		c.setFatal(fmt.Sprintf("temporary ban: code=%d", v.Code))
		log.Printf("TEMPORARY BAN from WhatsApp: code=%d expires=%s", v.Code, v.Expire)
	}
}

// ingestMessage archives one live (or history-replayed) message.
func (c *Client) ingestMessage(v *events.Message) {
	kind, text, ref := ExtractContent(v.Message)
	if kind == "" {
		return // protocol/system payload — not worth archiving
	}
	m := store.Message{
		ChatJID:   v.Info.Chat.String(),
		ID:        v.Info.ID,
		SenderJID: v.Info.Sender.ToNonAD().String(),
		Ts:        v.Info.Timestamp.Unix(),
		IsFromMe:  v.Info.IsFromMe,
		Kind:      kind,
		Text:      text,
		MediaRef:  ref,
	}
	if err := c.st.InsertMessage(m); err != nil {
		log.Printf("insert message %s/%s: %v", m.ChatJID, m.ID, err)
		return
	}
	// For direct chats an incoming push name doubles as the chat display name.
	chatName := ""
	if !v.Info.IsFromMe && !v.Info.IsGroup && v.Info.PushName != "" {
		chatName = v.Info.PushName
	}
	if err := c.st.UpsertChat(m.ChatJID, chatName, v.Info.IsGroup, m.Ts); err != nil {
		log.Printf("upsert chat %s: %v", m.ChatJID, err)
	}
	if !v.Info.IsFromMe && v.Info.PushName != "" {
		_ = c.st.UpsertContact(v.Info.Sender.ToNonAD().String(), v.Info.PushName, "", "")
	}
}

// ingestHistorySync archives the backlog the phone ships right after pairing.
// Depth is decided by the phone (typically recent messages, not the full archive).
func (c *Client) ingestHistorySync(v *events.HistorySync) {
	for _, conv := range v.Data.GetConversations() {
		chatJID, err := types.ParseJID(conv.GetID())
		if err != nil {
			continue
		}
		for _, hmsg := range conv.GetMessages() {
			parsed, err := c.WM.ParseWebMessage(chatJID, hmsg.GetMessage())
			if err != nil {
				continue // e.g. undecryptable placeholder — skip quietly
			}
			c.ingestMessage(parsed)
		}
		// Conversation metadata carries group subjects / saved chat names.
		if name := conv.GetName(); name != "" {
			_ = c.st.UpsertChat(chatJID.String(), name, chatJID.Server == types.GroupServer, 0)
		}
	}
	log.Printf("history sync chunk ingested (%d conversations)", len(v.Data.GetConversations()))
}

// syncContacts dumps whatsmeow's synced contact list into our archive so the
// gateway can resolve names → JIDs. Runs on every (re)connect; upserts only.
func (c *Client) syncContacts() {
	contacts, err := c.WM.Store.Contacts.GetAllContacts(context.Background())
	if err != nil {
		log.Printf("contact sync failed: %v", err)
		return
	}
	for jid, info := range contacts {
		_ = c.st.UpsertContact(jid.String(), info.PushName, info.FullName, info.BusinessName)
	}
	log.Printf("contact sync: %d contacts", len(contacts))
}
