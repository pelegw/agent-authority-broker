package wa

import (
	"testing"
)

func TestParseRecipient(t *testing.T) {
	cases := []struct {
		in      string
		wantJID string
		wantErr bool
	}{
		{"972501234567@s.whatsapp.net", "972501234567@s.whatsapp.net", false},
		{"12345-67890@g.us", "12345-67890@g.us", false},
		{"123456789012345@lid", "123456789012345@lid", false},                     // hidden-user chats
		{"972501234567:12@s.whatsapp.net", "972501234567@s.whatsapp.net", false},  // device suffix stripped
		{"123456789012345:9@lid", "123456789012345@lid", false},                   // lid device suffix stripped
		{"972501234567.0:2@s.whatsapp.net", "972501234567@s.whatsapp.net", false}, // agent+device stripped
		{"@lid", "", true}, // empty user must be rejected
		{"@s.whatsapp.net", "", true},
		{"+972501234567", "972501234567@s.whatsapp.net", false},
		{"972501234567", "972501234567@s.whatsapp.net", false},
		{" +972501234567 ", "972501234567@s.whatsapp.net", false}, // whitespace tolerated
		{"not-a-number", "", true},
		{"", "", true},
		{"hello@example.com", "", true}, // wrong server
	}
	for _, tc := range cases {
		jid, err := ParseRecipient(tc.in)
		if tc.wantErr {
			if err == nil {
				t.Errorf("%q: expected error, got %s", tc.in, jid)
			}
			continue
		}
		if err != nil {
			t.Errorf("%q: %v", tc.in, err)
			continue
		}
		if jid.String() != tc.wantJID {
			t.Errorf("%q gave %s, want %s", tc.in, jid, tc.wantJID)
		}
	}
}

func TestMediaTypeOfMapping(t *testing.T) {
	// Stickers download via the image endpoint; unknown kinds fall back to document.
	if mediaTypeOf("sticker") != mediaTypeOf("image") {
		t.Error("sticker should map to the image media type")
	}
	if mediaTypeOf("unknown") != mediaTypeOf("document") {
		t.Error("unknown should fall back to document")
	}
}
