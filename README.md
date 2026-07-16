# SplitLLM Backend

Async FastAPI backend for the SplitLLM expense-splitting app with Supabase JWT authentication, MongoDB, and Catalyst QuickML (GLM/Qwen) for natural-language expense parsing and receipt scanning. Deployed on Zoho Catalyst AppSail.

## Tech Stack

- **Python 3.12+** with **FastAPI**
- **Motor** (async MongoDB driver)
- **Supabase JWT** verification (asymmetric, via the project's public JWKS)
- **Catalyst QuickML LLM Serving** (GLM text + Qwen vision-language) for LLM inference
- **Pydantic v2** for request/response validation

## Quick Start

### 1. Prerequisites

- Python 3.12+
- MongoDB (local via Docker, or Atlas for production)
- A Supabase project (for auth)
- (Optional, for LLM/receipt features) Catalyst QuickML endpoints + a Zoho Self
  Client — the app runs fine with `LLM_ENABLED=false`

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
# Edit .env with your credentials (MONGODB_URI, SUPABASE_*, QUICKML_*, etc.)
```

**Authentication:**
- **Supabase:** Stateless JWT validation — the API verifies the Supabase access token
  (Bearer JWT) against `${SUPABASE_URL}/auth/v1/.well-known/jwks.json` and reads the
  user id from the `sub` claim.
- **QuickML (optional):** The LLM and receipt-scanning features call Catalyst
  QuickML endpoints, authenticated with a Zoho OAuth token (Self Client refresh
  flow). Left disabled (`LLM_ENABLED=false`) they are skipped entirely.

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

**Database (production):** Create a free **MongoDB Atlas M0** cluster, add a
database user, and (for the M0 free tier) allow access from `0.0.0.0/0` (auth is
via the connection string). Use the SRV connection string as `MONGODB_URI`.

> The production deploy path is **Zoho Catalyst AppSail** — see the section below.

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
| `LLM_ENABLED` | Enable Catalyst QuickML LLM/receipt features | `false` |
| `QUICKML_LLM_ENDPOINT` | QuickML GLM text endpoint (required when LLM enabled) | (empty) |
| `QUICKML_VLM_ENDPOINT` | QuickML Qwen vision endpoint (receipts) | (empty) |
| `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` / `OAUTH_REFRESH_TOKEN` | Zoho Self Client for QuickML auth | (empty) |
| `CORS_ORIGINS` | Comma-separated allowed origins (add the hosting domain) | `http://localhost:3000,...` |
| `HOST` | Server host (local only) | `0.0.0.0` |
| `PORT` | Server port (AppSail injects its own listen port) | `8081` |

> In production these live in `catalyst/appsail/app-config.json` `env_variables`
> and the Catalyst console (secrets never enter the bundle or repo). Locally they
> all come from `.env`. See `.env.example` for the full list.

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
