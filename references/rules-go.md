# Go Rules

<!-- Managed by vibe-coding-harness -->

- Follow the existing Go module and workspace boundaries; do not create unnecessary modules.
- Keep commands under `cmd/` and non-public application packages under `internal/` when starting a conventional service.
- Define interfaces at the consumer and keep them minimal; prefer concrete types until substitution is required.
- Pass `context.Context` explicitly across request and I/O boundaries; never store it in a struct.
- Wrap errors with operation context, preserve causes, and handle each error once at the owning layer.
- Give every goroutine an owner, cancellation path, and observable termination condition.
- Protect shared state deliberately and run race-sensitive tests when concurrency changes.
- Keep zero values useful where practical and avoid configuration hidden in package globals.
- Prefer table-driven tests for meaningful cases, but keep failure names and assertions readable.
- Run formatting, `go vet`, affected tests, `go test ./...`, race checks when relevant, and `go build ./...`.
