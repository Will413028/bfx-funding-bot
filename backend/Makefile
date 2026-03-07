.PHONY: dev build sqlc migrate-diff migrate-apply

dev:
	FRONTEND_URL=http://localhost:3000 go run ./cmd/server/

build:
	go build -o bin/server ./cmd/server/

sqlc:
	sqlc generate

migrate-diff:
	cd schema && atlas migrate diff $(name) --env neon --dir "file://../migrations"

migrate-apply:
	cd schema && atlas migrate apply --env neon --dir "file://../migrations"
