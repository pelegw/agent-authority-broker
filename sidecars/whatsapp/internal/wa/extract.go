package wa

import (
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"

	"aab/sidecars/whatsapp/internal/store"
)

// downloadable is the subset of media proto getters needed to build a
// store.MediaRef (matches whatsmeow's DownloadableMessage shape).
type downloadable interface {
	GetDirectPath() string
	GetMediaKey() []byte
	GetFileSHA256() []byte
	GetFileEncSHA256() []byte
	GetFileLength() uint64
}

// ExtractContent flattens a WhatsApp message proto into (kind, text, mediaRef).
// kind == "" means "nothing worth archiving" (receipts, key distribution, etc).
// Pure function — the unit tests build protos and assert on the outcome.
func ExtractContent(msg *waE2E.Message) (kind, text string, ref *store.MediaRef) {
	if msg == nil {
		return "", "", nil
	}
	// Unwrap container messages down to their real payload.
	if e := msg.GetEphemeralMessage(); e != nil {
		return ExtractContent(e.GetMessage())
	}
	if v := msg.GetViewOnceMessage(); v != nil {
		return ExtractContent(v.GetMessage())
	}
	if v := msg.GetViewOnceMessageV2(); v != nil {
		return ExtractContent(v.GetMessage())
	}
	if d := msg.GetDocumentWithCaptionMessage(); d != nil {
		return ExtractContent(d.GetMessage())
	}

	switch {
	case msg.GetConversation() != "":
		return "text", msg.GetConversation(), nil
	case msg.GetExtendedTextMessage().GetText() != "":
		return "text", msg.GetExtendedTextMessage().GetText(), nil
	case msg.GetImageMessage() != nil:
		m := msg.GetImageMessage()
		return "image", m.GetCaption(), mediaRef("image", m.GetMimetype(), "", m)
	case msg.GetVideoMessage() != nil:
		m := msg.GetVideoMessage()
		return "video", m.GetCaption(), mediaRef("video", m.GetMimetype(), "", m)
	case msg.GetAudioMessage() != nil:
		m := msg.GetAudioMessage()
		return "audio", "", mediaRef("audio", m.GetMimetype(), "", m)
	case msg.GetDocumentMessage() != nil:
		m := msg.GetDocumentMessage()
		return "document", m.GetCaption(), mediaRef("document", m.GetMimetype(), m.GetFileName(), m)
	case msg.GetStickerMessage() != nil:
		m := msg.GetStickerMessage()
		return "sticker", "", mediaRef("sticker", m.GetMimetype(), "", m)
	case msg.GetReactionMessage() != nil:
		// Text is the emoji; an empty emoji means "reaction removed" — skip.
		if t := msg.GetReactionMessage().GetText(); t != "" {
			return "reaction", t, nil
		}
	}
	return "", "", nil
}

func mediaRef(mediaType, mime, fileName string, d downloadable) *store.MediaRef {
	return &store.MediaRef{
		MediaType:     mediaType,
		MimeType:      mime,
		FileName:      fileName,
		DirectPath:    d.GetDirectPath(),
		MediaKey:      d.GetMediaKey(),
		FileSHA256:    d.GetFileSHA256(),
		FileEncSHA256: d.GetFileEncSHA256(),
		FileLength:    d.GetFileLength(),
	}
}
