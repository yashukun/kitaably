# Kitaably
#
# The world comes up in two commands. Everything is in compose (DECISIONS.md D33)
# except the model server, which runs on YOUR MACHINE rather than in a container:
#     make setup-llm   then   make up

SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- local platform ---------------------------------------------------------

.PHONY: setup-llm
setup-llm: ## Install Ollama models on THIS machine (not in a container)
	@command -v ollama >/dev/null 2>&1 || { \
		echo "Ollama is not installed."; \
		echo "  macOS:  brew install ollama   (or https://ollama.com/download)"; \
		echo "  Linux:  curl -fsSL https://ollama.com/install.sh | sh"; \
		exit 1; }
	@echo "Pulling models onto the host..."
	ollama pull llama3.2:3b
	@echo
	@echo "Now make sure Ollama is SERVING: open the app, or run 'ollama serve'."
	@echo "The stack reaches it at host.docker.internal:11434 (see .env)."
	@curl -sf -m 3 http://127.0.0.1:11434/api/version >/dev/null \
		&& echo "OK - Ollama is answering on 11434." \
		|| echo "NOT RUNNING - start it before 'make up', or generation will fail."

.PHONY: llm-check
llm-check: ## Is the host's Ollama up, and can the containers reach it?
	@curl -sf -m 3 http://127.0.0.1:11434/api/version \
		&& echo " <- host OK" \
		|| { echo "Ollama is not answering on the host. Run 'ollama serve'."; exit 1; }
	@docker compose exec -T backend python -c \
		"import httpx,os; u=os.environ['OPENAI_BASE_URL'].rsplit('/v1',1)[0]; \
		 print(httpx.get(u+'/api/version', timeout=5).text, '<- reachable from containers')" \
		2>/dev/null || echo "Containers cannot reach it. Is the stack up?"

.PHONY: up
up: ## Build and start EVERYTHING -- Supabase included (DECISIONS.md D33)
	docker compose up --build

.PHONY: down
down: ## Stop them
	docker compose down

.PHONY: clean
clean: ## Stop them and drop their volumes -- INCLUDING THE DATABASE
	@echo "This deletes the Postgres volume: every book, assessment and account."
	@printf "Type 'yes' to continue: " && read ans && [ "$$ans" = "yes" ]
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
	@test -n "$(name)" || { echo "usage: make migration name=add_books"; exit 1; }
	@f="supabase/migrations/$$(date -u +%Y%m%d%H%M%S)_$(name).sql"; \
		printf -- "-- %s\n\n" "$(name)" > "$$f"; echo "created $$f"

.PHONY: db-push
db-push: ## Apply migrations to the linked project
	supabase db push

.PHONY: db-reset
db-reset: ## Rebuild the local database from migrations, then seed
	@echo "This drops the database volume and rebuilds it from scratch."
	@printf "Type 'yes' to continue: " && read ans && [ "$$ans" = "yes" ]
	docker compose rm -sf supabase-db supabase-bootstrap supabase-seed
	docker volume rm -f kitaably_supabase-db-data
	docker compose up --build -d supabase-bootstrap supabase-seed

# --build because the migrations are baked into the bootstrap image (supabase/Dockerfile),
# not bind-mounted: a new file under supabase/migrations/ is not in the container until
# the image is rebuilt. The rebuild is a COPY and takes seconds.
.PHONY: migrate
migrate: ## Apply any migrations not yet applied, and re-assert the buckets (no data loss)
	docker compose up --build supabase-bootstrap

.PHONY: seed
seed: ## Load test accounts (skipped if auth.users is not empty)
	docker compose up --build supabase-seed

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
