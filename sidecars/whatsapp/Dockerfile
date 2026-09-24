# Build: pure-Go (modernc sqlite), so no C toolchain needed.
FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /out/sidecar .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates tzdata
# Run unprivileged (uid 10001, same as plugin-whatsapp, which mounts /data read-only).
# /data (the archive, messages.db) and /session (whatsmeow's session.db, the
# WhatsApp credential) are created and owned BEFORE the VOLUME line: changes
# after VOLUME are discarded, and a fresh named volume inherits this ownership
# and mode. /session is 0700 and is a volume only this container mounts.
RUN addgroup -S -g 10001 aab \
 && adduser -S -D -H -u 10001 -G aab -s /sbin/nologin aab \
 && mkdir -p /data /session \
 && chown aab:aab /data /session \
 && chmod 0700 /session
COPY --from=build /out/sidecar /usr/local/bin/sidecar
# Set here as well as in compose, so the image never falls back to keeping
# the session in DATA_DIR (see internal/config).
ENV SESSION_DIR=/session
USER aab
VOLUME /data /session
ENTRYPOINT ["sidecar"]
