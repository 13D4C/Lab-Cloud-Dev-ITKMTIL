package database

import (
	"context"
	"log"
)

func Bootstrap() {
	ctx := context.Background()
	pool, err := DB.DB()
	if err != nil {
		log.Fatal(err)
	}
	connection, err := pool.Conn(ctx)
	if err != nil {
		log.Fatal(err)
	}
	defer connection.Close()
	if _, err := connection.ExecContext(ctx, "SELECT pg_advisory_lock(73403130)"); err != nil {
		log.Fatal(err)
	}
	defer connection.ExecContext(ctx, "SELECT pg_advisory_unlock(73403130)")
	Migrate()
	Seed()
}
