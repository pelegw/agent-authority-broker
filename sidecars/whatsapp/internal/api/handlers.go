package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"aab/sidecars/whatsapp/internal/wa"
)

type handlers struct{ wa WhatsApp }

func (h *handlers) health(rw http.ResponseWriter, _ *http.Request) {
	writeJSON(rw, http.StatusOK, map[string]string{"status": "ok"})
}

func (h *handlers) status(rw http.ResponseWriter, _ *http.Request) {
	writeJSON(rw, http.StatusOK, h.wa.Status())
}

// qr serves the current pairing code as a PNG so the human can log in from a
// browser (proxied by the gateway's admin API).
func (h *handlers) qr(rw http.ResponseWriter, _ *http.Request) {
	png, err := h.wa.QRPNG()
	switch {
	case errors.Is(err, wa.ErrLoggedIn):
		writeError(rw, http.StatusConflict, err.Error())
	case errors.Is(err, wa.ErrNoQR):
		writeError(rw, http.StatusServiceUnavailable, err.Error())
	case err != nil:
		writeError(rw, http.StatusInternalServerError, err.Error())
	default:
		rw.Header().Set("Content-Type", "image/png")
		_, _ = rw.Write(png)
	}
}

type sendRequest struct {
	To   string `json:"to"`
	Text string `json:"text"`
}

func (h *handlers) send(rw http.ResponseWriter, r *http.Request) {
	var req sendRequest
	r.Body = http.MaxBytesReader(rw, r.Body, 64*1024) // text messages only
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(rw, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if strings.TrimSpace(req.To) == "" || req.Text == "" {
		writeError(rw, http.StatusBadRequest, "both 'to' and 'text' are required")
		return
	}
	res, err := h.wa.SendText(r.Context(), req.To, req.Text)
	switch {
	case errors.Is(err, wa.ErrNotLinked):
		writeError(rw, http.StatusServiceUnavailable, err.Error())
	case err != nil:
		// Includes bad recipients; the gateway shows the message to the admin.
		writeError(rw, http.StatusBadGateway, err.Error())
	default:
		writeJSON(rw, http.StatusOK, res)
	}
}

func (h *handlers) media(rw http.ResponseWriter, r *http.Request) {
	chatJID := r.URL.Query().Get("chat_jid")
	messageID := r.URL.Query().Get("message_id")
	if chatJID == "" || messageID == "" {
		writeError(rw, http.StatusBadRequest, "chat_jid and message_id query params are required")
		return
	}
	data, mime, err := h.wa.MediaByMessage(r.Context(), chatJID, messageID)
	switch {
	case errors.Is(err, wa.ErrNotFound), errors.Is(err, wa.ErrNoMedia):
		writeError(rw, http.StatusNotFound, err.Error())
	case err != nil:
		writeError(rw, http.StatusBadGateway, err.Error())
	default:
		if mime == "" {
			mime = "application/octet-stream"
		}
		rw.Header().Set("Content-Type", mime)
		_, _ = rw.Write(data)
	}
}

func writeJSON(rw http.ResponseWriter, status int, v any) {
	rw.Header().Set("Content-Type", "application/json")
	rw.WriteHeader(status)
	_ = json.NewEncoder(rw).Encode(v)
}

func writeError(rw http.ResponseWriter, status int, msg string) {
	writeJSON(rw, status, map[string]string{"error": msg})
}
