package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"aab/sidecars/whatsapp/internal/wa"
)

// fakeWA scripts the WhatsApp client behavior per test.
type fakeWA struct {
	status   wa.Status
	qrPNG    []byte
	qrErr    error
	sendRes  wa.SendResult
	sendErr  error
	lastTo   string
	lastText string
	media    []byte
	mime     string
	mediaErr error
}

func (f *fakeWA) Status() wa.Status      { return f.status }
func (f *fakeWA) QRPNG() ([]byte, error) { return f.qrPNG, f.qrErr }
func (f *fakeWA) SendText(_ context.Context, to, text string) (wa.SendResult, error) {
	f.lastTo, f.lastText = to, text
	return f.sendRes, f.sendErr
}
func (f *fakeWA) MediaByMessage(_ context.Context, _, _ string) ([]byte, string, error) {
	return f.media, f.mime, f.mediaErr
}

const token = "secret-token"

func doReq(t *testing.T, h http.Handler, method, path, tok, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if tok != "" {
		req.Header.Set("X-Internal-Token", tok)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestTokenRequiredOnEverythingButHealth(t *testing.T) {
	h := NewHandler(token, &fakeWA{})
	if rec := doReq(t, h, "GET", "/health", "", ""); rec.Code != 200 {
		t.Errorf("/health without token: %d, want 200", rec.Code)
	}
	for _, path := range []string{"/status", "/qr", "/media"} {
		if rec := doReq(t, h, "GET", path, "", ""); rec.Code != 401 {
			t.Errorf("GET %s without token: %d, want 401", path, rec.Code)
		}
		if rec := doReq(t, h, "GET", path, "wrong", ""); rec.Code != 401 {
			t.Errorf("GET %s with wrong token: %d, want 401", path, rec.Code)
		}
	}
	if rec := doReq(t, h, "POST", "/send", "", `{"to":"x","text":"y"}`); rec.Code != 401 {
		t.Errorf("POST /send without token: %d, want 401", rec.Code)
	}
}

func TestStatusReturnsClientState(t *testing.T) {
	f := &fakeWA{status: wa.Status{Connected: true, LoggedIn: true, JID: "me@s.whatsapp.net"}}
	rec := doReq(t, NewHandler(token, f), "GET", "/status", token, "")
	if rec.Code != 200 {
		t.Fatalf("status: %d", rec.Code)
	}
	var got wa.Status
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if !got.LoggedIn || got.JID != "me@s.whatsapp.net" {
		t.Errorf("unexpected status payload: %+v", got)
	}
}

func TestQRStatuses(t *testing.T) {
	cases := []struct {
		name string
		f    *fakeWA
		want int
	}{
		{"logged in", &fakeWA{qrErr: wa.ErrLoggedIn}, 409},
		{"no code yet", &fakeWA{qrErr: wa.ErrNoQR}, 503},
		{"available", &fakeWA{qrPNG: []byte("png")}, 200},
	}
	for _, tc := range cases {
		rec := doReq(t, NewHandler(token, tc.f), "GET", "/qr", token, "")
		if rec.Code != tc.want {
			t.Errorf("%s: %d, want %d", tc.name, rec.Code, tc.want)
		}
	}
}

func TestSendValidatesAndForwards(t *testing.T) {
	f := &fakeWA{sendRes: wa.SendResult{MessageID: "ABC", Ts: 42}}
	h := NewHandler(token, f)

	if rec := doReq(t, h, "POST", "/send", token, `not json`); rec.Code != 400 {
		t.Errorf("bad json: %d, want 400", rec.Code)
	}
	if rec := doReq(t, h, "POST", "/send", token, `{"to":"","text":"hi"}`); rec.Code != 400 {
		t.Errorf("empty to: %d, want 400", rec.Code)
	}
	rec := doReq(t, h, "POST", "/send", token, `{"to":"972501234567","text":"hi"}`)
	if rec.Code != 200 {
		t.Fatalf("send: %d body=%s", rec.Code, rec.Body.String())
	}
	if f.lastTo != "972501234567" || f.lastText != "hi" {
		t.Errorf("send forwarded %q/%q", f.lastTo, f.lastText)
	}

	f.sendErr = wa.ErrNotLinked
	if rec := doReq(t, h, "POST", "/send", token, `{"to":"x","text":"y"}`); rec.Code != 503 {
		t.Errorf("not linked: %d, want 503", rec.Code)
	}
}

func TestMediaErrors(t *testing.T) {
	if rec := doReq(t, NewHandler(token, &fakeWA{}), "GET", "/media", token, ""); rec.Code != 400 {
		t.Errorf("missing params: %d, want 400", rec.Code)
	}
	f := &fakeWA{mediaErr: wa.ErrNotFound}
	if rec := doReq(t, NewHandler(token, f), "GET", "/media?chat_jid=c&message_id=m", token, ""); rec.Code != 404 {
		t.Errorf("not found: %d, want 404", rec.Code)
	}
	f = &fakeWA{media: []byte("bytes"), mime: "image/jpeg"}
	rec := doReq(t, NewHandler(token, f), "GET", "/media?chat_jid=c&message_id=m", token, "")
	if rec.Code != 200 || rec.Header().Get("Content-Type") != "image/jpeg" {
		t.Errorf("media ok: code=%d ct=%q", rec.Code, rec.Header().Get("Content-Type"))
	}
}
