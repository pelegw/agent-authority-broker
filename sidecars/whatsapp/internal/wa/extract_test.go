package wa

import (
	"testing"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
)

func TestExtractPlainText(t *testing.T) {
	kind, text, ref := ExtractContent(&waE2E.Message{Conversation: proto.String("hello")})
	if kind != "text" || text != "hello" || ref != nil {
		t.Errorf("got %q %q %v", kind, text, ref)
	}
}

func TestExtractExtendedText(t *testing.T) {
	msg := &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("linked text")}}
	kind, text, _ := ExtractContent(msg)
	if kind != "text" || text != "linked text" {
		t.Errorf("got %q %q", kind, text)
	}
}

func TestExtractImageBuildsMediaRef(t *testing.T) {
	msg := &waE2E.Message{ImageMessage: &waE2E.ImageMessage{
		Caption:       proto.String("look!"),
		Mimetype:      proto.String("image/jpeg"),
		DirectPath:    proto.String("/v/t62"),
		MediaKey:      []byte{1, 2, 3},
		FileSHA256:    []byte{4},
		FileEncSHA256: []byte{5},
		FileLength:    proto.Uint64(1234),
	}}
	kind, text, ref := ExtractContent(msg)
	if kind != "image" || text != "look!" {
		t.Fatalf("got %q %q", kind, text)
	}
	if ref == nil || ref.DirectPath != "/v/t62" || ref.FileLength != 1234 || ref.MimeType != "image/jpeg" {
		t.Errorf("bad media ref: %+v", ref)
	}
}

func TestExtractUnwrapsEphemeral(t *testing.T) {
	inner := &waE2E.Message{Conversation: proto.String("disappearing")}
	msg := &waE2E.Message{EphemeralMessage: &waE2E.FutureProofMessage{Message: inner}}
	kind, text, _ := ExtractContent(msg)
	if kind != "text" || text != "disappearing" {
		t.Errorf("got %q %q", kind, text)
	}
}

func TestExtractReaction(t *testing.T) {
	thumbsUp := "\U0001F44D"
	msg := &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Text: proto.String(thumbsUp)}}
	if kind, text, _ := ExtractContent(msg); kind != "reaction" || text != thumbsUp {
		t.Errorf("got %q %q", kind, text)
	}
	// Reaction removal (empty text) is noise; skip it.
	removal := &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Text: proto.String("")}}
	if kind, _, _ := ExtractContent(removal); kind != "" {
		t.Errorf("removal should be skipped, got kind %q", kind)
	}
}

func TestExtractSkipsEmptyAndNil(t *testing.T) {
	if kind, _, _ := ExtractContent(nil); kind != "" {
		t.Errorf("nil: %q", kind)
	}
	if kind, _, _ := ExtractContent(&waE2E.Message{}); kind != "" {
		t.Errorf("empty: %q", kind)
	}
}
