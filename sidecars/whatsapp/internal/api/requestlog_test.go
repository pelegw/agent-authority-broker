package api

import (
	"bytes"
	"errors"
	"log"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"aab/sidecars/whatsapp/internal/wa"
)

// captureLog sends the standard logger to a buffer for one test.
func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	flags := log.Flags()
	log.SetOutput(&buf)
	log.SetFlags(0)
	t.Cleanup(func() {
		log.SetOutput(os.Stderr)
		log.SetFlags(flags)
	})
	return &buf
}

// An error whose text quotes the recipient, as a real bad-recipient error does.
var errBadRecipient = errors.New("invalid recipient 972501234567")

func doReqWithID(t *testing.T, method, path, tok, body, rid string, f *fakeWA) {
	t.Helper()
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	if tok != "" {
		r.Header.Set("X-Internal-Token", tok)
	}
	if rid != "" {
		r.Header.Set("X-Request-Id", rid)
	}
	NewHandler(token, f).ServeHTTP(httptest.NewRecorder(), r)
}

func TestSendIsLoggedWithoutTextOrRecipient(t *testing.T) {
	buf := captureLog(t)
	f := &fakeWA{sendRes: wa.SendResult{MessageID: "3EB0ABC", Ts: 42}}
	doReqWithID(t, "POST", "/send", token,
		`{"to":"972501234567","text":"the secret message body"}`, "req-7.a_b", f)
	out := buf.String()
	for _, want := range []string{
		"request method=POST path=/send status=200", "request_id=req-7.a_b",
		"send result=ok message_id=3EB0ABC",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("log lacks %q:\n%s", want, out)
		}
	}
	for _, never := range []string{"secret message", "972501234567", token} {
		if strings.Contains(out, never) {
			t.Errorf("log contains %q:\n%s", never, out)
		}
	}
}

func TestFailedSendLogsTheStatusNotTheError(t *testing.T) {
	buf := captureLog(t)
	f := &fakeWA{sendErr: errBadRecipient}
	doReqWithID(t, "POST", "/send", token, `{"to":"972501234567","text":"hi there"}`, "", f)
	out := buf.String()
	if !strings.Contains(out, "send result=error status=502") ||
		!strings.Contains(out, "status=502") {
		t.Errorf("missing failure lines:\n%s", out)
	}
	if strings.Contains(out, "972501234567") || strings.Contains(out, "hi there") {
		t.Errorf("failure line leaks the request:\n%s", out)
	}
}

func TestMediaIsLoggedWithoutItsQuery(t *testing.T) {
	buf := captureLog(t)
	f := &fakeWA{media: []byte("bytes"), mime: "image/jpeg"}
	doReqWithID(t, "GET", "/media?chat_jid=chat123%40g.us&message_id=msg456", token, "", "", f)
	out := buf.String()
	if !strings.Contains(out, "path=/media status=200") {
		t.Errorf("missing request line:\n%s", out)
	}
	if strings.Contains(out, "chat123") || strings.Contains(out, "msg456") ||
		strings.Contains(out, "bytes") {
		t.Errorf("request line leaks the query or body:\n%s", out)
	}
}

func TestAMalformedRequestIDIsNotLogged(t *testing.T) {
	buf := captureLog(t)
	doReqWithID(t, "GET", "/status", token, "", "bad id=forged", &fakeWA{})
	out := buf.String()
	if !strings.Contains(out, "request_id=-") || strings.Contains(out, "forged") {
		t.Errorf("request id not refused:\n%s", out)
	}
}

func TestAnInjectedPathStaysOnOneLine(t *testing.T) {
	buf := captureLog(t)
	doReqWithID(t, "GET", "/x%0Arequest%20method=GET%20status=200", token, "", "", &fakeWA{})
	out := strings.TrimRight(buf.String(), "\n")
	if strings.Count(out, "\n") != 0 {
		t.Errorf("one request produced several lines:\n%s", out)
	}
	if !strings.Contains(out, `path="/x\nrequest method=GET status=200"`) {
		t.Errorf("path not quoted and escaped:\n%s", out)
	}
}

func TestRefusedTokenIsLoggedWithoutTheToken(t *testing.T) {
	buf := captureLog(t)
	doReqWithID(t, "GET", "/status", "wrong-token-value", "", "", &fakeWA{})
	out := buf.String()
	if !strings.Contains(out, "request refused: bad or missing X-Internal-Token path=/status") ||
		!strings.Contains(out, "status=401") {
		t.Errorf("missing refusal lines:\n%s", out)
	}
	if strings.Contains(out, "wrong-token-value") {
		t.Errorf("refusal logged the presented token:\n%s", out)
	}
}

func TestHealthAndQuietAreNotLogged(t *testing.T) {
	buf := captureLog(t)
	doReqWithID(t, "GET", "/health", "", "", "", &fakeWA{})
	Quiet = true
	t.Cleanup(func() { Quiet = false })
	doReqWithID(t, "GET", "/status", token, "", "", &fakeWA{})
	if buf.Len() != 0 {
		t.Errorf("expected no lines, got:\n%s", buf.String())
	}
}

func TestQRServedIsLoggedWithoutTheCode(t *testing.T) {
	buf := captureLog(t)
	doReqWithID(t, "GET", "/qr", token, "", "", &fakeWA{qrPNG: []byte("PNGDATA")})
	out := buf.String()
	if !strings.Contains(out, "qr served") || strings.Contains(out, "PNGDATA") {
		t.Errorf("qr line wrong:\n%s", out)
	}
}

func TestValueQuotesOnlyWhatNeedsIt(t *testing.T) {
	cases := map[string]string{
		"/send": "/send", "": "-", "a b": `"a b"`, `q"x`: `"q\"x"`, "k=v": `"k=v"`,
		"line\nbreak": `"line\nbreak"`,
	}
	for in, want := range cases {
		if got := Value(in); got != want {
			t.Errorf("Value(%q) = %s, want %s", in, got, want)
		}
	}
}
