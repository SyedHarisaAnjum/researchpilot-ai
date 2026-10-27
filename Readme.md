# ResearchLab: an autonomous AI research agent

You ask a question. The agent plans the research, searches the web, reads the pages, pulls out
evidence that it checks word for word against the source, looks for gaps, searches again where
needed, and writes a report in which every factual claim is cited and checked against its
source. Reports can be downloaded as PDF, Markdown or JSON.

- **Backend:** Python, FastAPI, LangGraph, Google Gemini (LLM), Tavily (web search), SQLAlchemy, Alembic, Arq + Redis
- **Frontend:** React 19, Vite, React Router
- **Production:** Docker Compose with PostgreSQL, Redis, an API, a background worker, and Nginx

---

## Contents

1. [How it works, end to end](#1-how-it-works-end-to-end)
2. [What was built, phase by phase](#2-what-was-built-phase-by-phase)
3. [Project layout](#3-project-layout)
4. [API](#4-api)
5. [Security and cost controls](#5-security-and-cost-controls)
6. [Running locally](#6-running-locally)
7. [Deploying to production](#7-deploying-to-production)
8. [Configuration](#8-configuration)
9. [Testing and evaluation](#9-testing-and-evaluation)
10. [Operations](#10-operations)
11. [Known limits](#11-known-limits)

---

## 1. How it works, end to end

```
Browser (React)
   │  sign in → session cookie
   │  POST /api/research {question, depth}
   ▼
Nginx ──/api──► FastAPI (API)  ──creates job row──► PostgreSQL
                     │                                  ▲
                     └──enqueue job ID──► Redis ──► Worker (Arq)
                                                        │ runs the LangGraph research graph,
                                                        │ writes progress events, sources,
                                                        │ evidence and the report
   ◄── GET /api/research/{id}/events (Server-Sent Events, read from the database)
   ◄── GET /api/research/{id}/report, /sources, /api/reports/{id}/download?format=pdf
```

**One research job, step by step**

1. **Plan** (`plan_research`): Gemini turns the question into 2–8 subquestions, 3–12 search queries and a research goal (structured JSON, validated by Pydantic).
2. **Search** (`search_web`): Tavily runs this round's queries. Results are normalised and de-duplicated by URL (case, `www.`, tracking parameters and fragments are ignored).
3. **Read** (`read_pages`): pages are fetched with SSRF protection, cleaned of navigation, scripts and banners, and saved as **source records** (`S01`, `S02`, …) with a retrieval time and, if the page states one, a publication date. Blocked or unsupported pages are recorded, not hidden.
4. **Extract** (`extract_evidence`): for each page, Gemini lists claims with a supporting quote. **Code checks that each quote really appears in the page**; invented quotes are thrown away. The result is a set of **evidence records** (`E001`, …), each tied to its source.
5. **Evaluate gaps** (`evaluate_gaps`): code counts coverage per subquestion (supported, weak or missing). Gemini then reviews authority, conflicts, currency and uncertainty. A subquestion with no evidence always stays open, whatever the model says.
6. **Next round or stop**: code, not the model, decides whether to continue, based on open questions and the rounds, searches, pages, model calls, tokens and time left. A new round gets new queries; queries already used and URLs already read are never repeated.
7. **Write the report** (`finalize_report`): Gemini drafts statements marked *fact* or *interpretation*. Citations are checked: unknown source IDs and unknown URLs are removed. **A second Gemini pass checks each fact against the excerpts it cites.** Unsupported facts are flagged and left out of the findings. Methodology, limitations and references are built by code from the stored records.
8. **Save and stream**: progress events are stored in the database and streamed to the browser over SSE. Sources, evidence and the report are saved, and the report renders as Markdown and PDF.

---

## 2. What was built, phase by phase

| Phase | What it covers | Main files |
|---|---|---|
| 1 Setup | FastAPI app, config validated at startup, CORS, a consistent error format, health check | `app/main.py`, `app/config.py` |
| 3 LLM | Async Gemini client: retries for temporary errors, timeouts, structured output validated by Pydantic (retried with feedback when invalid), token and latency logging, a cap on thinking tokens | `app/services/llm_service.py` |
| 4 Search & reading | Tavily behind a provider interface; a web reader that blocks SSRF (private IPs, metadata hosts, redirects; connects to the IP that was checked), extracts the main text and publication date, and limits size | `search_service.py`, `web_reader.py`, `utils/url_safety.py` |
| 5 Planning | Prompts for each step, with rules against invented facts and with page text fenced off as untrusted; a JSON-serialisable state | `agent/prompts.py`, `agent/state.py` |
| 6 Graph | LangGraph nodes and conditional edges, bounded rounds and budgets, no repeated searches, a fallback plan for temporary failures | `agent/nodes.py`, `agent/graph.py`, `agent/quality.py`, `agent/limits.py` |
| 7 Citations | Separate source and evidence records, stable IDs, citation and URL checks, a second-pass support check, facts kept apart from interpretation, publication dates kept apart from retrieval dates | `services/report_service.py` |
| 8 Persistence | SQLAlchemy models and Alembic migrations; jobs, sources, evidence, reports and events | `db/`, `migrations/` |
| 8 API | Job endpoints, SSE, report download, cancellation | `api/` |
| 9 Frontend | Dashboard, form, live progress, report viewer with citation links, source list, history | `frontend/src/` |
| 10 Streaming | SSE with stored events, a `research_state` snapshot on every (re)connect, `Last-Event-ID` resume | `api/events.py`, `hooks/useResearchJob.js` |
| 11 Testing | 150+ backend tests (mocked: no API quota), frontend tests, an evaluation set of 20 questions with a scoring script | `tests/`, `frontend/src/**/*.test.jsx`, `evals/` |
| 12 Safeguards | Token budget, rate limits, prompt-injection tests, secret redaction in logs, error codes, cancellation that stops API calls | `utils/run_context.py`, `services/rate_limit.py` |
| 13 Reports | Markdown and PDF (headings, tables, clickable references, citation links); the PDF renderer can't fetch anything | `services/pdf_service.py` |
| Production | Accounts and sessions, per-user data, CSRF protection, a durable Arq queue, Redis rate limits, PostgreSQL, Docker Compose, Nginx, JSON logs, readiness checks | `auth/`, `api/auth.py`, `worker.py`, `docker-compose.yml` |

---

## 3. Project layout

```
backend/
  app/
    main.py              FastAPI app: middleware (CSRF, security headers), errors, routers, health/ready
    config.py            All settings (from env / .env), production checks
    logging_setup.py     Job ID on every line, secrets redacted, optional JSON
    worker.py            Arq worker that runs research jobs
    cli.py               Account management (create the first admin)
    auth/                Password hashing, sessions, current-user dependencies
    api/                 auth, research, events (SSE), report (downloads), debug
    agent/               LangGraph graph, nodes, prompts, state, limits, quality checks
    services/            llm, search, web reader, report, pdf, jobs, rate limits
    db/                  Engine/sessions and models
    utils/               URL safety, per-job context (job ID, token usage)
  migrations/            Alembic migrations (applied by `alembic upgrade head`)
  tests/                 pytest suite with fake LLM/search/reader
  evals/                 20-question evaluation set and scoring script
frontend/
  src/                   React app (pages, components, hooks, services/api.js, auth/)
  nginx.conf             Production web server: static app plus /api proxy, SSE, security headers
docker-compose.yml       Production stack
.env.production.example  Production settings template
```

---

## 4. API

All endpoints except `/api/health`, `/api/ready` and `/api/auth/login` require a signed-in
session. State-changing requests must send `X-Requested-With: ResearchLab`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness |
| GET | `/api/ready` | Readiness: database, queue and Redis reachable (503 if not) |
| POST | `/api/auth/login` | `{email, password}` → sets the session cookie |
| POST | `/api/auth/logout` | Ends the session |
| GET | `/api/auth/me` | The signed-in user |
| GET/POST | `/api/admin/users` | List / create accounts (admins) |
| PATCH | `/api/admin/users/{id}` | Change name, password, admin role, active (admins) |
| POST | `/api/research` | `{question, depth: quick\|standard\|deep}` → 202 with the job |
| GET | `/api/research` | Your jobs (`?limit&offset&status`) |
| GET | `/api/research/{id}` | Status, stage, progress, live counts, limits, error code |
| GET | `/api/research/{id}/events` | SSE progress stream (resumable) |
| GET | `/api/research/{id}/report` | The report (409 while running) |
| GET | `/api/research/{id}/sources` | Every page tried, with status and evidence count |
| POST | `/api/research/{id}/cancel` | Stop a job |
| GET | `/api/reports/{id}/download?format=pdf\|markdown\|json` | Download |

Errors always have the shape `{"error": {"code", "message", "details"}}`. Interactive docs are at
`/api/docs` in development (in production only with `ENABLE_DOCS=true`).

---

## 5. Security and cost controls

| Area | How it's handled |
|---|---|
| Accounts | Created by admins only. Passwords hashed with scrypt (salted). Sessions are random tokens stored only as SHA-256, in an `HttpOnly`, `Secure`, `SameSite=Strict` cookie. Deactivating an account or changing its password signs it out everywhere. |
| Sign-in abuse | Rate limited per account and per IP. A single generic error, and equal timing whether or not the account exists. |
| CSRF | `SameSite=Strict` cookie plus a required custom header on state-changing requests. |
| Data access | Users see only their own research; other users' job IDs return 404. Admins see all. |
| SSRF | Only http(s) on standard ports; private, loopback, link-local, metadata and CGNAT addresses blocked after DNS; the connection is pinned to the checked IP; each redirect is checked again; the PDF renderer never fetches. |
| Prompt injection | Page text is wrapped in `<source>` tags and treated as data; the model has no tools; every claim needs a quote found in the page; citations and URLs are checked against stored records. |
| Output validation | All model output is parsed with Pydantic (bounded retries with feedback). Gemini gets a simplified schema; Pydantic enforces the full one. |
| Budgets | Per job: rounds, searches, pages, model calls, **tokens**, time. Per user: 5 jobs a minute, 30 an hour, 3 running at once. Server-wide: 10 queued or running. |
| Errors | Each failure has a code: `llm_timeout`, `llm_rate_limited`, `llm_invalid_output`, `llm_error`, `search_*`, `cancelled`, `interrupted`, `internal_error`. Reports flag `partial` and `insufficient_evidence`. |
| Secrets | Server-side only; a blank key stops startup; keys are redacted from logs; `.env` files are git-ignored. |
| Headers | API: `nosniff`, `DENY` framing, `no-referrer`, `no-store`. Nginx adds a strict Content-Security-Policy. |
| Reliability | Durable queue (Redis with append-only persistence). Jobs return to the queue if the worker stops and are retried up to 3 times; cancellation reaches a job in any process within a second. |

---

## 6. Running locally

Requirements: Python 3.10+ and Node 20.19+.

```powershell
# Backend
cd backend
python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy ..\.env.production.example .env     # then set GEMINI_API_KEY, TAVILY_API_KEY, GEMINI_MODEL
#   (for local use, remove POSTGRES_PASSWORD; SQLite and in-process jobs are the defaults)
python -m app.cli create-user --email you@example.com --name "You" --admin
uvicorn app.main:app --reload --port 8000

# Frontend (second terminal)
cd frontend
npm install
npm run dev                              # http://localhost:5173 (proxies /api to :8000)
```

Locally the defaults are SQLite (`backend/research.db`), jobs running inside the API process,
in-memory rate limits, and API docs at http://localhost:8000/api/docs.

---

## 7. Deploying to production

On a server with Docker:

```bash
cp .env.production.example .env        # fill in GEMINI_API_KEY, TAVILY_API_KEY, POSTGRES_PASSWORD
docker compose up -d --build
docker compose run --rm api python -m app.cli create-user --email you@example.com --name "You" --admin
```

Then open `http://<server>:8080` (or `http://localhost:8080`).

What starts:

| Service | Role |
|---|---|
| `postgres` | Database (volume `pgdata`) |
| `redis` | Job queue and shared rate limits (append-only persistence, volume `redisdata`) |
| `migrate` | Runs `alembic upgrade head` once, before the API and worker start |
| `api` | FastAPI behind `uvicorn --workers 2 --proxy-headers` (not exposed directly) |
| `worker` | Arq worker running research jobs (`MAX_CONCURRENT_JOBS` at a time) |
| `web` | Nginx: the built React app plus `/api` proxy, SSE unbuffered, security headers |

**HTTPS is required** for anything other than localhost: session cookies are `Secure`. Put a TLS
terminator in front of port 8080, for example Caddy (`your.domain { reverse_proxy localhost:8080 }`),
a cloud load balancer, or Cloudflare.

The app refuses to start in production with SQLite, without Redis, with in-process jobs, or
with insecure cookies.

**Updating:** `git pull && docker compose up -d --build`. Migrations run automatically through the `migrate` service.

**Scaling:** run more workers with `docker compose up -d --scale worker=3`. API processes can also be
scaled, since rate limits and jobs live in Redis and PostgreSQL.

---

## 8. Configuration

Every setting can come from the environment or `backend/.env` (local). The main ones:

| Setting | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY`, `TAVILY_API_KEY` | (required) | Must not be blank |
| `GEMINI_MODEL` | (required) | e.g. `gemini-2.5-flash` |
| `ENVIRONMENT` | `development` | `production` turns on the safety checks above |
| `DATABASE_URL` | `sqlite+aiosqlite:///./research.db` | Production: `postgresql+asyncpg://…` |
| `JOB_BACKEND` | `inprocess` | `arq` in production |
| `REDIS_URL` | – | Required in production |
| `RUN_MIGRATIONS_ON_STARTUP` | `true` | `false` in Compose (the `migrate` service does it) |
| `MAX_CONCURRENT_JOBS` | 2 | Jobs per worker at once |
| `MAX_ACTIVE_JOBS`, `MAX_ACTIVE_JOBS_PER_USER` | 10, 3 | Queued + running |
| `RESEARCH_JOBS_PER_MINUTE`, `…_PER_HOUR` | 5, 30 | Per user |
| `LOGIN_ATTEMPTS_PER_15_MINUTES` | 10 | Per account (×3 per IP) |
| `SESSION_DAYS` | 14 | |
| Standard-depth budget | 3 rounds, 9 searches, 10 pages, 30 calls, 500k tokens, 300 s | `MAX_RESEARCH_ROUNDS`, `MAX_TOTAL_SEARCHES`, `MAX_PAGES_PER_RESEARCH`, `MAX_LLM_CALLS`, `MAX_TOTAL_TOKENS`, `MAX_RESEARCH_SECONDS` |
| `LLM_THINKING_BUDGET` | 1024 | Thinking tokens per call (they count against the output limit) |
| `LOG_FORMAT` | `text` | `json` in Compose |
| `ENABLE_DOCS` | on except in production | `/api/docs` |
| `PDF_FONT_PATH` | Bitstream Vera | A TTF with wider character coverage (e.g. Noto Sans) |

Quick and deep depths have fixed budgets (`app/agent/limits.py`).

---

## 9. Testing and evaluation

```powershell
cd backend;  python -m pytest              # 150+ tests, about 15 s, no API calls
cd frontend; npm test                      # Vitest + Testing Library
```

Tests use fake LLM, search and reader services, a throwaway database and fake Redis. They cover:

- configuration, the LLM service, search and the reader (including SSRF)
- the graph: transitions and budgets
- evidence and citations
- the database surviving restarts
- the API, SSE, accounts and access control, CSRF
- rate limits, the queue's retry and cancellation, PDF output, and end to end

The one live test runs only with `RUN_LIVE_TESTS=1`.

**Evaluation** (`backend/evals/`): 20 questions (5 factual, 5 comparative, 5 needing recent
information, 5 with conflicting or insufficient sources). The script records:

- relevance (an LLM judge told to ignore length and tone) and expected key terms
- citation coverage and support
- completeness and freshness
- uncertainty handling
- efficiency and reliability, including runs with injected failures

```powershell
python -m evals.run_eval --mode mock --fault-rate 0.3   # free
python -m evals.run_eval --mode live --ids F2 C5 X4     # real APIs, a few questions at a time
```

See `backend/evals/README.md`.

---

## 10. Operations

- **Logs:** `docker compose logs -f api worker`. In Compose they're JSON lines with `job` IDs; API keys are redacted.
- **Health:** `/api/health` (process up) and `/api/ready` (database, queue and Redis reachable). Containers have health checks.
- **Backups:** `docker compose exec postgres pg_dump -U research research > backup.sql`. Restore with `psql`. Back up the `redisdata` volume if queued jobs matter.
- **Accounts:** `docker compose run --rm api python -m app.cli list-users`, and `set-password --email …`. Admins can also use the **Users** page.
- **Costs:** each job is capped by its depth budget. Watch the `tokens` and `llm_calls` in each job's stats, and the provider dashboards (Google AI Studio, Tavily).

---

## 11. Known limits

- A job that is interrupted restarts from the beginning; there's no mid-graph checkpointing.
- Pages that need JavaScript, and PDFs, aren't read (they're recorded as failed or unsupported).
- The support check and the judge are Gemini themselves: an aid, not proof. Read the cited sources for anything important.
- A publication date is whatever the page states; for wikis it's often when the page was created.
- The PDF font covers Latin text and common symbols; set `PDF_FONT_PATH` for other scripts.
- There's no public sign-up, email verification or password reset by email (admins reset passwords).
