package api

// One log line per API request: method, path, status, duration and the
// caller's request id. plugin-whatsapp sends the broker's X-Request-Id, so
// a send's line here carries the same id as the broker's decision row and
// the plugin's /perform line.
//
// Never a body, never the query string (/media's names a chat and a
// message), never a header value other than the validated request id.

import (
	"log"
	"net/http"
	"regexp"
	"strconv"
	"time"
)

// Quiet turns the per-request lines off (LOG_LEVEL=WARN or ERROR).
var Quiet = false

// The shape the broker and the plugin runtime accept; anything else is
// logged as "-" so a caller cannot put text into the line.
var requestIDRe = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)

// RequestID is the caller's X-Request-Id when well formed, else "-".
func RequestID(r *http.Request) string {
	if id := r.Header.Get("X-Request-Id"); requestIDRe.MatchString(id) {
		return id
	}
	return "-"
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusRecorder) Write(b []byte) (int, error) {
	if s.status == 0 {
		s.status = http.StatusOK
	}
	return s.ResponseWriter.Write(b)
}

// withRequestLog wraps the route table. /health (a liveness probe) is not
// logged.
func withRequestLog(next http.Handler) http.Handler {
	return http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		if Quiet || r.URL.Path == "/health" {
			next.ServeHTTP(rw, r)
			return
		}
		started := time.Now()
		rec := &statusRecorder{ResponseWriter: rw}
		next.ServeHTTP(rec, r)
		if rec.status == 0 {
			rec.status = http.StatusOK
		}
		log.Printf("request method=%s path=%s status=%d duration_ms=%d request_id=%s",
			Value(r.Method), Value(r.URL.Path), rec.status,
			time.Since(started).Milliseconds(), RequestID(r))
	})
}

var bareValue = regexp.MustCompile(`^[!#-<>-\[\]-~]+$`)

// Value renders a string as a logfmt value, like the Python services' kv():
// bare when it holds only printable ASCII other than space, quote, "=" and
// backslash, else quoted and escaped, so a crafted path can neither end the
// line nor forge another field.
func Value(s string) string {
	if s == "" {
		return "-"
	}
	if bareValue.MatchString(s) {
		return s
	}
	return strconv.Quote(s)
}
