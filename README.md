# SplitLLM Backend

Async FastAPI backend for the SplitLLM expense-splitting app with Supabase JWT authentication, MongoDB, and Google Vertex AI (Gemini) for natural language expense parsing.

## Tech Stack

- **Python 3.12+** with **FastAPI**
- **Motor** (async MongoDB driver)
- **Supabase JWT** verification (asymmetric, via the project's public JWKS)
- **Google Vertex AI** (`google-genai` with Vertex AI backend) for LLM inference
- **Pydantic v2** for request/response validation

## Quick Start

### 1. Prerequisites

- Python 3.12+
- MongoDB (local via Docker, or Atlas for production)
- A Supabase project (for auth)
- A GCP project with Vertex AI API enabled
- `gcloud` CLI for local auth (`gcloud auth application-default login`)

### 2. Setup

```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate  # macOS/Linux

# Install dependencies (runtime + test tooling)
pip install -r requirements-dev.txt   # or `-r requirements.txt` for runtime only

# Configure environment
cp .env.example .env
# Edit .env with your credentials (MONGODB_URI, SUPABASE_*, GCP_PROJECT_ID, etc.)
```

**Authentication:**
- **Supabase:** Stateless JWT validation — the API verifies the Supabase access token
  (Bearer JWT) against `${SUPABASE_URL}/auth/v1/.well-known/jwks.json` and reads the
  user id from the `sub` claim.
- **Vertex AI:** Uses Application Default Credentials (ADC). Locally, run
  `gcloud auth application-default login`. On Cloud Run, the service account provides
  credentials automatically — just grant it the `roles/aiplatform.user` IAM role.

### 3. Run

```bash
# Development (with auto-reload)
uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload

# Production-style
uvicorn app.main:app --host 0.0.0.0 --port 8081 --workers 4
```

### 4. Docker (local dev)

```bash
docker-compose up -d   # starts the API + a local MongoDB
```

The `docker-compose.yml` runs a local MongoDB 7.0 container with a named volume
(`mongo_data`) for data persistence. Data survives `docker-compose down` but is
only stored locally — use MongoDB Atlas for production.

## Deploy to Google Cloud Run

The API ships as a container ([`Dockerfile`](./Dockerfile)) that listens on `$PORT`
(Cloud Run injects `8080`). Connect your repository to Cloud Run for automatic
deployment on every commit.

**Database:** Create a free **MongoDB Atlas M0** cluster, add a database user, and
(for the M0 free tier) allow access from `0.0.0.0/0` (auth is via the connection
string). Use the SRV connection string as `MONGODB_URI`.

**Vertex AI:** The Cloud Run service account needs the `roles/aiplatform.user` IAM
role. No API key is required — authentication is handled via ADC.

**Required Cloud Run Environment Variables:**

| Variable | Type | Description |
|---|---|---|
| `MONGODB_URI` | Secret | MongoDB Atlas SRV connection string |
| `GCP_PROJECT_ID` | Env var | GCP project ID for Vertex AI |
| `GCP_LOCATION` | Env var | Vertex AI region (e.g. `global`) |
| `GEMINI_MODEL` | Env var | Model name (default: `gemini-2.0-flash`) |
| `DB_NAME` | Env var | Database name (default: `splitllm`) |
| `SUPABASE_URL` | Env var | Supabase project URL |
| `SUPABASE_ANON_KEY` | Env var | Supabase publishable / anon key |
| `CORS_ORIGINS` | Env var | Comma-separated allowed origins |

> **Service-to-service:** Another backend can call this API at its public URL. To make
> a call privately/without exposing it, grant that service's account the
> `roles/run.invoker` role and send a Google-signed ID token — Cloud Run handles the
> IAM check. (Keep `--allow-unauthenticated` while the browser frontend needs direct
> access.)

## API Documentation

Once running, visit:
- **Swagger UI**: http://localhost:8081/docs
- **ReDoc**: http://localhost:8081/redoc

## API Endpoints

| Category | Method | Endpoint | Auth |
|----------|--------|----------|------|
| Auth | POST | `/db/login` | ✅ |
| Auth | POST | `/db/signup` | ✅ |
| Auth | POST | `/db/logout` | ✅ |
| Auth | PUT | `/db/changePassword` | ✅ |
| Account | GET | `/db/user/account` | ✅ |
| Account | PUT | `/db/user/account` | ✅ |
| Dashboard | POST | `/db/dashboard/summary` | ✅ |
| Dashboard | POST | `/db/dashboard/chart` | ✅ |
| Events | GET | `/db/user/events` | ✅ |
| Events | GET | `/db/event/:id` | ✅ |
| Events | POST | `/db/event` | ✅ |
| Events | PUT | `/db/event` | ✅ |
| Events | DELETE | `/db/event/:id` | ✅ |
| Events | GET | `/db/event/:id/expenses` | ✅ |
| Events | GET | `/db/user/event/:id/dues` | ✅ |
| Expenses | POST | `/db/expense` | ✅ |
| Expenses | GET | `/db/expense/:id` | ✅ |
| Expenses | PUT | `/db/expense/:id` | ✅ |
| Expenses | DELETE | `/db/expense/:id` | ✅ |
| Context | GET | `/db/:type/:id/users` | ✅ |
| Personal | POST | `/db/expenses/personal` | ✅ |
| LLM | POST | `/llm/expense` | ✅ |
| Friends | GET | `/db/user/friends` | ✅ |
| Friends | POST | `/db/addFriend` | ✅ |
| Friends | DELETE | `/db/deleteFriend` | ✅ |
| Friends | GET | `/db/user/expense/friend/:id` | ✅ |
| Friends | POST | `/db/user/expense/friend/:id/settleup` | ✅ |
| Payments | GET | `/db/payment/:link` | ❌ |

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MONGODB_URI` | MongoDB connection string (Atlas SRV in prod) | `mongodb://localhost:27017` |
| `DB_NAME` | Database name | `splitllm` |
| `SUPABASE_URL` | Supabase project URL (used to fetch the JWKS) | `https://your-project.supabase.co` |
| `SUPABASE_ANON_KEY` | Supabase publishable / anon key | (set per environment) |
| `GCP_PROJECT_ID` | GCP project ID for Vertex AI | (required for LLM) |
| `GCP_LOCATION` | Vertex AI region | `global` |
| `GEMINI_MODEL` | Gemini model name | `gemini-2.0-flash` |
| `CORS_ORIGINS` | Comma-separated allowed origins (add your Vercel URL) | `http://localhost:3000,...` |
| `HOST` | Server host (local only) | `0.0.0.0` |
| `PORT` | Server port (Cloud Run injects `8080`) | `8081` |

> On Cloud Run, `MONGODB_URI` should be provided via **Secret Manager**;
> the rest are plain env vars. Locally they all come from `.env`.

## Deployment (Zoho Catalyst AppSail)

The API runs as an AppSail service in the Catalyst project **SplitLLM**
(`46831000000013050`, org `60078340202`, IN DC). Config lives in `catalyst/`:

```
catalyst/
├── catalyst.json          # Catalyst project config (appsail only)
├── .catalystignore
├── appsail/
│   ├── app-config.json    # runtime: stack, startup command, env vars
│   └── build/             # generated bundle (scripts/build_appsail.sh)
└── scripts/
    └── build_appsail.sh   # app/ + vendored deps -> appsail/build
```

### Deploy

```bash
cd catalyst
catalyst project:use SplitLLM --org 60078340202   # one-time binding
catalyst deploy                                   # predeploy runs build_appsail.sh
```

The AppSail URL is `https://splitllm-api-50044020391.development.catalystappsail.in`
(`/health` should return ok). CI deploys automatically on push to `main`
(see `.github/workflows/deploy.yml`; needs the `CATALYST_TOKEN` repo secret
from `catalyst token:generate`).

### Platform gotchas (hard-won — do not re-learn)

- **Env var names must not start with `ZOHO_` or `CATALYST_`** — AppSail
  rejects them with a 400 "reserved keywords" error. `app/config.py` maps the
  `QUICKML_*` / `OAUTH_*` env names onto the original field names via
  Pydantic `AliasChoices`.
- **The startup command must be shell-wrapped**:
  `sh -c '... --port ${X_ZOHO_CATALYST_LISTEN_PORT:-9000}'`. Without `sh -c`
  the port variable is not expanded and the app fails with "check the startup
  command or port".
- **Python deps must be vendored** into the bundle. Catalyst never installs
  `requirements.txt`; `build_appsail.sh` runs `pip install -t` with
  `--platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all:`.
- Secrets/env values live in `catalyst/appsail/app-config.json`
  `env_variables` (placeholders) and the Catalyst console — never in the
  bundle or repo `.env`.

### Free-tier watchpoints

- AppSail: 15 GB-hours/month free — check Billing → Usage after the first week.
- QuickML LLM serving is token-billed; expect the ~$5/project/month minimum
  once it exceeds the free tier.
