package store

import (
	"database/sql"

	"modernc.org/sqlite"
)

func init() {
	// We use modernc.org/sqlite (pure Go — no CGO, so tests run anywhere and
	// the Docker build needs no C toolchain), but whatsmeow's sqlstore and our
	// own code ask for driver "sqlite3" (the classic mattn name). Register the
	// modernc driver under that name; nothing else in this binary claims it.
	sql.Register("sqlite3", &sqlite.Driver{})
}
