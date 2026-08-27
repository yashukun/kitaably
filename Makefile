# Kitaably
#
# The world comes up in two commands, because Supabase runs its own stack:
#     make supabase   then   make up

SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- local platform ---------------------------------------------------------

.PHONY: supabase
supabase: ## Start the Supabase CLI stack (Postgres+pgvector, Auth, Storage, Studio)
	supabase start

.PHONY: supabase-stop
supabase-stop: ## Stop the Supabase CLI stack
	supabase stop

.PHONY: up
up: ## Build and start the services this repo owns
	docker compose up --build

.PHONY: down
down: ## Stop them
	docker compose down

.PHONY: clean
clean: ## Stop them and drop their volumes (redis, model cache, ollama models)
	docker compose down -v

.PHONY: logs
logs: ## Tail all service logs
	docker compose logs -f

.PHONY: ps
ps: ## Show service status
	docker compose ps

# --- database ---------------------------------------------------------------

.PHONY: migration
migration: ## Create a migration:  make migration name=add_books
	supabase migration new $(name)

.PHONY: db-push
db-push: ## Apply migrations to the linked project
	supabase db push

.PHONY: db-reset
db-reset: ## Rebuild the local database from migrations, then seed
	supabase db reset

.PHONY: seed
seed: ## Load test accounts and sample material
	supabase db reset

.PHONY: reingest
reingest: ## Re-chunk and re-embed every book (run after CHUNK_TOKENS or EMBEDDING_MODEL changes)
	docker compose exec -T worker python -m app.workers.reingest

# --- development (outside containers) ---------------------------------------

.PHONY: sync
sync: ## Install every dependency
	cd backend && uv sync --extra dev
	cd embeddings && uv sync --extra dev
	cd frontend && npm install

.PHONY: dev-backend
dev-backend: ## Run the API with reload
	cd backend && uv run uvicorn app.main:app --reload --port 8000

.PHONY: dev-worker
dev-worker: ## Run a Celery worker across all queues
	cd backend && uv run celery -A app.workers.celery_app.celery_app worker \
		--loglevel=info -Q ingest,llm,proctor,maintenance

.PHONY: dev-embeddings
dev-embeddings: ## Run the embeddings service
	cd embeddings && uv run uvicorn app.main:app --reload --port 8001

.PHONY: dev-frontend
dev-frontend: ## Run Next.js
	cd frontend && npm run dev

# --- quality ----------------------------------------------------------------

.PHONY: fmt
fmt: ## Format
	cd backend && uv run ruff format . && uv run ruff check --fix .
	cd embeddings && uv run ruff format . && uv run ruff check --fix .

.PHONY: lint
lint: ## Lint and typecheck
	cd backend && uv run ruff check . && uv run mypy app
	cd embeddings && uv run ruff check .
	cd frontend && npm run lint && npx tsc --noEmit

.PHONY: test
test: ## Run every test suite
	cd backend && uv run pytest
	cd embeddings && uv run pytest

.PHONY: check
check: lint test ## Everything CI will run
