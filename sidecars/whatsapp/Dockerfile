# Build: pure-Go (modernc sqlite), so no C toolchain needed.
FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /out/sidecar .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates tzdata
COPY --from=build /out/sidecar /usr/local/bin/sidecar
VOLUME /data
ENTRYPOINT ["sidecar"]
