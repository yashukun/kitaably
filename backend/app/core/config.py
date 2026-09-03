"""Application settings.

The one place environment is read. Nothing else calls ``os.getenv`` — adding a key
means adding it here, to ``.env.example``, and to the Kustomize ConfigMap/Secret
template in the same change.

Keys mirror ``.env.example`` exactly. ``NEXT_PUBLIC_*`` variables live in the same
file but belong to the browser bundle, so ``extra="ignore"`` lets them pass through
without becoming backend config.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Repo-root .env first, then a service-local one if it exists (later wins).
        # Lets `uvicorn` run from backend/ and from the repo root identically; in a
        # container the values arrive as real environment variables instead.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---------------------------------------------------------------
    app_name: str = "Kitaably"
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # --- Supabase ----------------------------------------------------------
    supabase_url: str = ""
    supabase_anon_key: str = ""
    # BYPASSES RLS. Backend and worker only. Never reaches a browser bundle.
    supabase_service_role_key: str = ""
    supabase_jwks_url: str = ""
    supabase_jwt_issuer: str = ""
    supabase_jwt_audience: str = "authenticated"
    bucket_books: str = "books"
    bucket_evidence: str = "evidence"

    # --- Database ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:54322/postgres"
    db_pool_size: int = 10
    db_max_overflow: int = 5

    # --- Redis / Celery ----------------------------------------------------
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    # --- Embeddings service ------------------------------------------------
    embeddings_url: str = "http://embeddings:8001"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    embedding_batch_size: int = 64
    # bge-small-en-v1.5 is a 512-token model and SILENTLY TRUNCATES past it: a longer
    # passage returns a vector byte-identical to its own first 512 tokens, so the tail
    # is not merely weighted less, it is absent from the index. This is the ceiling
    # `chunk_tokens` must respect (DECISIONS.md D21).
    embedding_max_tokens: int = 512
    # bge is an ASYMMETRIC model: passages are embedded bare, queries are embedded
    # with this instruction in front. Using the same form for both is the documented
    # misuse and costs real recall. Empty string disables it for a symmetric model.
    embedding_query_prefix: str = "Represent this sentence for searching relevant passages: "

    # --- LLM (OpenAI-compatible) -------------------------------------------
    openai_base_url: str = "http://ollama:11434/v1"
    openai_api_key: str = "ollama"
    llm_model: str = "llama3.2:3b"
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 3
    # Ask the provider to constrain replies that get parsed to a single JSON object.
    # Ollama and OpenAI both honour it; a provider that does not 400s on the field and
    # the client retries once without it. Off only to reproduce the un-constrained
    # failure mode, which is the one that used to lose five generation calls in eight.
    llm_json_mode: bool = True

    # The model for work that runs in a WORKER rather than in front of a person.
    # Blank means "the same one", which is the default and keeps a single-model setup
    # single-model.
    #
    # Two workloads, opposite trade-offs, and one setting could not serve both. Chat
    # has somebody watching a cursor blink, so it wants the fastest model that is good
    # enough — D21 chose llama3.2:3b for exactly that, measured. Generation and grading
    # run on the `llm` queue where nobody is watching, take minutes either way, and are
    # judged on whether the distractors are plausible and the rubric is sane. Making
    # the tutor twice as slow to get better exam questions is a bad trade; making
    # generation twice as slow is often a good one.
    #
    # BLANK on purpose, and the blank is a measurement rather than a default nobody
    # revisited. qwen2.5:7b was tried here and reverted: on this box it generated at
    # ~1 tok/s against a 180s per-call timeout, so EVERY generation call timed out and
    # a paper came back empty -- slower than the 3B by enough to be useless, not by
    # enough to be a trade. The cause is the Docker VM, not the model: Ollama holding
    # two models sat at 7.9 GiB of an 11.67 GiB VM while the host itself ran at ~2x
    # oversubscription, so the weights were competing for memory that was not there.
    #
    # Raise this to a bigger model when the box can feed one -- check
    # `docker stats kitaably-ollama-1` sits well under the VM limit first, and expect
    # to raise ASSESSMENT_LLM_TIMEOUT_SECONDS with it.
    #
    # Not a reasoning model, whatever the size. deepseek-r1 and its kin were considered
    # and rejected on mechanics rather than taste: generation calls pass
    # json_object=True, and a JSON grammar cannot emit a <think> block -- so the
    # reasoning is either suppressed (paying 8B latency for no reasoning) or it lands
    # inside the reply, where assessment_reply_max_tokens is spent on thought instead of
    # questions and the first-brace parsers in services/assessments.py and
    # services/grading.py lock onto a brace in the thinking. Nothing here strips it.
    llm_generation_model: str = ""

    @property
    def generation_model(self) -> str:
        """The model assessment generation, harvesting and grading actually use."""
        return self.llm_generation_model.strip() or self.llm_model
    # Hard ceiling on an answer. Generation is the slower half of a CPU model, and
    # it is paid per token: uncapped, the tutor writes until it runs out of things to
    # say, and the reader waits for prose they stopped reading three paragraphs ago.
    llm_max_answer_tokens: int = 500

    # --- RAG tuning --------------------------------------------------------
    # Sized to the EMBEDDER, not to the reader. It was 800, which is above
    # `embedding_max_tokens`, so 79% of the library was being indexed on its first
    # 512 tokens with the remainder invisible to search. The token count here is the
    # cheap chars//4 estimate, which under-counts dense technical prose, so this
    # leaves real headroom under 512 rather than sitting on it (DECISIONS.md D21).
    chunk_tokens: int = 320
    chunk_overlap_tokens: int = 64
    # How many passages the tutor finally sees. Every one of these is prompt the
    # model must read before it may write a word, and prompt evaluation is linear in
    # its length -- so this number is a latency dial as much as a quality one.
    retrieval_top_k: int = 5
    # How many the *database* returns before ranking narrows them. Chunk overlap
    # means the top hits are routinely the same passage shown twice, so dedupe, book
    # routing and page spread all need more candidates than they will keep. Raised
    # alongside the smaller chunks: the same amount of book is now more rows.
    retrieval_candidate_k: int = 30
    # HNSW candidate list size, set per transaction before a vector search. pgvector
    # POST-filters, so scope and book narrowing are applied to whatever the index
    # hands back -- too low and a filtered search silently returns fewer rows than
    # asked for, which reads as "my book doesn't cover that".
    retrieval_ef_search: int = 120
    # Tokens of each retrieved passage actually shown to the model. The chunk stays
    # whole in the database, in the ranking and in the citation the reader opens --
    # only the copy in the prompt is excerpted, to the sentences that bear on the
    # question (app/rag/trim.py). Prompt evaluation is linear in length and is the
    # larger half of the wait on a CPU model, and most of a 320-token passage is not
    # answering anything. 0 disables excerpting and sends the passage whole.
    retrieval_source_tokens: int = 140
    # Cosine distance ceiling for the FIRST pass. Nothing inside it and the search
    # falls to the salvage tier below rather than straight to a refusal.
    retrieval_max_distance: float = 0.35
    # NOT a second, laxer retrieval ceiling -- there is no such number. Measured,
    # bge-small puts on-topic questions at 0.17-0.29 and questions the book has
    # nothing on at 0.36-0.45: no gap, so widening the SEARCH cannot separate
    # "barely covered" from "not covered".
    #
    # This is a CORROBORATION ceiling instead, applied to the handful of passages
    # the reader's own selective words named (rag/retrieve.py ::
    # search_chunks_corroborated). Scored against the question as typed, those
    # separate cleanly -- the passage that answers it at 0.36-0.38, one that
    # merely shares a word at 0.46-0.61 -- and 0.42 sits in that gap. It is safe
    # at this width only because nothing reaches it that the question did not
    # name (DECISIONS.md D31).
    retrieval_salvage_distance: float = 0.42
    # Lexical candidates for a mention question ("find every mention of X"), fused
    # with the vector hits (app/rag/shape.py, D22). Wider than TOP_K because for a
    # mention question the list of occurrences IS the product, not an input to a
    # synthesis -- and a lexical row is cheap, there being no embedding to ship.
    retrieval_lexical_k: int = 20
    # Chat costs money or CPU on every call, so it is capped per user per minute.
    chat_rate_limit_per_minute: int = 20

    # --- Chat conversation -------------------------------------------------
    # Turns of transcript given to the tutor for continuity, and to the condenser so
    # a follow-up can be resolved. Six is three exchanges: enough for "explain that
    # again", short enough that the transcript never crowds out the sources.
    chat_history_turns: int = 4
    # Share of the retrieval vote at which one book is treated as *the* source for a
    # question. See app/rag/rank.py -- too low silently drops the book that had the
    # answer, so it errs high.
    chat_book_dominance: float = 0.62
    # Characters of each earlier ASSISTANT turn kept for continuity. The tutor needs
    # to remember what it covered, not to re-read its own prose.
    chat_history_reply_chars: int = 240
    # Ask the model to label a message the rules could not. Off makes every ambiguous
    # message a question, which is the safe direction and costs only a wasted search.
    chat_intent_llm_fallback: bool = False
    # Passages shown to the model for a whole-book question (overview) or a
    # holistic comparison, split across the target books. Same latency dial as
    # RETRIEVAL_TOP_K -- every passage is prompt the model reads before writing --
    # but an overview needs breadth where a focused answer needs precision.
    chat_overview_sources: int = 10
    # Passages shown for a mention question after fusing lexical and vector hits.
    # Slightly wider than TOP_K because a mention answer enumerates locations
    # rather than synthesising one explanation.
    chat_lookup_sources: int = 8
    # Most books one overview or comparison covers in a single answer. More than
    # this and each book's share of the prompt is too thin to say anything true;
    # the rest are named in the answer and can be asked about by name.
    chat_multibook_max: int = 4
    # Rewrite a follow-up into a standalone question with the MODEL rather than with
    # the deterministic fallback in services/chat.py. Off by default: it is a whole
    # extra generation on the critical path, serial and before retrieval even starts,
    # and the fallback (glue the previous question onto this one) retrieves nearly as
    # well for a fraction of a second's work instead of tens of seconds (D21).
    chat_condense_llm: bool = False
    # Classify a book's kind/genre/summary at the end of ingest. Best effort: a
    # failure leaves the columns null and changes nothing about retrieval.
    book_classification_enabled: bool = True

    # --- Assessments -------------------------------------------------------
    # Chunks per LLM call. Batching gives the model enough context to avoid asking
    # the same thing twice within a batch; one call per question does not.
    assessment_batch_chunks: int = 5
    # Validation and dedupe reject some, so sample more chunks than questions wanted.
    assessment_oversample: float = 1.5
    # A chunk this short cannot support a question worth asking.
    assessment_min_chunk_tokens: int = 60
    # Cosine similarity above which two stems are the same question. Near-duplicates
    # are the single most common complaint about a generated paper.
    assessment_dedupe_similarity: float = 0.92
    # Generation and grading both cost money or CPU, so both are capped per user.
    # A hard ceiling on LLM calls per paper. Generation asks once per format and then
    # backfills whatever fell short, so without a cap a stubborn model and a thin book
    # can spin.
    #
    # Twelve, not twenty, and the number is set by the slowest provider rather than the
    # best one. Measured here: llama3.2:3b on CPU generates at ~9.5 tokens/second, so a
    # batch of five passages costs one to two minutes — twenty calls is over half an
    # hour of a spinner. Twelve leaves the seven-format auto mix one attempt each and
    # five calls of backfill, which is where most of the recovery happens anyway.
    #
    # Raise it when OPENAI_BASE_URL points at something fast; it is a wall-clock budget,
    # not a quality setting.
    assessment_max_llm_calls: int = 12

    # The reply ceiling for one generation call, enforced by the provider (Ollama maps
    # max_tokens to num_predict). Sized for a batch of three or four questions with
    # room to spare; what it exists to stop is the runaway reply — eighteen questions
    # for two asked, observed in a real run — which at ~9.5 tokens/second of CPU is
    # minutes spent writing output the validator then rejects. A reply the ceiling
    # truncates is a skipped batch the backfill pass recovers; a reply with no ceiling
    # is unbounded wall clock.
    assessment_reply_max_tokens: int = 800

    # Generation's own request timeout, above the client-wide default. A batch prompt
    # is five passages, and on CPU-only Ollama prompt evaluation plus a full reply sits
    # right at the general 120s — so generation calls kept dying at 119s with a
    # complete answer nearly in hand, then being retried from zero. Paired with
    # retries=0: one honest attempt with room to finish, and the backfill pass is the
    # retry policy.
    assessment_llm_timeout_seconds: int = 180

    assessment_rate_limit_per_hour: int = 10

    # --- Assessments: reading the author's brief ---------------------------
    # Whether an unparsed brief gets a model call to read it. Rules first, exactly as
    # chat intent does (D23): "focus on chapter 3" and "use the questions from the
    # book" are patterns, not judgement calls, and a rule costs no wall clock. The
    # model tail is for the briefs the rules do not recognise, and it is off by
    # default because on CPU it is another minute before the first question.
    assessment_brief_llm: bool = False
    # How many chunks a named topic pulls into the pool, per topic. The brief narrows
    # the pool; it never replaces it — a paper still has to span the material.
    assessment_topic_chunks: int = 30

    # --- Assessments: the book's own questions -----------------------------
    # The share of a paper that may be taken from questions the BOOK asks, when the
    # material actually carries them. Not a target and not a floor: it is a ceiling on
    # how much of a paper is other people's writing. The rest is authored, because a
    # paper that is entirely the back-of-chapter exercises is a photocopy.
    #
    # Zero disables harvesting entirely; 1.0 lets a paper be all book questions when
    # the author explicitly asks for that in their brief.
    assessment_book_question_share: float = 0.4
    # A chunk needs this many recognisable questions in it before it is worth spending
    # a call on. One stray question mark in a paragraph of prose is not an exercise
    # set, and asking the model to harvest from prose returns invented questions
    # wearing a book's clothes.
    assessment_min_harvest_questions: int = 2

    # A `generating` row whose last write is older than this is dead, not slow.
    # Generation checkpoints its trace onto the row after every stage and every LLM
    # call, one call is bounded by assessment_llm_timeout_seconds, and the failure
    # handler writes a reason for any failure it survives to see. What it cannot
    # survive is the worker being killed mid-run — `docker compose down`, a closed
    # laptop — which leaves the row generating for ever, refusing every edit and
    # reporting nothing. The maintenance sweep returns such rows to draft with a
    # reason. Fifteen minutes is five times the longest silent stretch a live run
    # can produce.
    assessment_stale_after_seconds: int = 900

    # --- Uploads (untrusted input) -----------------------------------------
    max_upload_mb: int = 80
    max_page_count: int = 1200
    allowed_source_formats: str = "pdf,docx,pptx,txt,md,zip"
    # A ZIP is one book uploaded in parts (D26), combined at parse time. The wire
    # cap above bounds only the compressed bytes, and a ZIP entry's declared size
    # is attacker-controlled, so the decompressed total gets its own cap, enforced
    # on the bytes actually produced.
    zip_max_members: int = 64
    zip_max_uncompressed_mb: int = 320

    # --- Proctoring --------------------------------------------------------
    evidence_retention_days: int = 60
    heartbeat_interval_seconds: int = 15
    # Silence longer than this becomes a heartbeat_gap event. Absence is evidence.
    heartbeat_gap_seconds: int = 60
    # How often the browser flushes its event queue. Served to the client at
    # session open, so tuning capture cadence is a config change, not a release.
    proctor_event_batch_seconds: int = 10
    # An active session silent this long is over: closed as 'aborted' by the sweep
    # and aggregated, so an abandoned attempt still reaches the review queue.
    proctor_abandon_seconds: int = 1800

    # --- URLs / CORS -------------------------------------------------------
    frontend_url: str = "http://localhost:3000"
    backend_internal_url: str = "http://backend:8000"
    cors_origins: str = "http://localhost:3000"

    # Comma-separated env values are kept as strings and split here rather than
    # typed as list[str]: pydantic-settings would otherwise try to JSON-decode them.

    @property
    def allowed_source_formats_list(self) -> list[str]:
        return [
            fmt.strip().lower()
            for fmt in self.allowed_source_formats.split(",")
            if fmt.strip()
        ]

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def zip_max_uncompressed_bytes(self) -> int:
        return self.zip_max_uncompressed_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Read once, reuse everywhere."""
    return Settings()


settings = get_settings()
