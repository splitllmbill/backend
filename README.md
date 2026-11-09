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
- **QR/device-code login:** set `SUPABASE_SERVICE_ROLE_KEY` and make sure
  `APP_BASE_URL` is the public Flutter web origin. The backend holds each
  one-time handoff for at most ten minutes; the service-role key is never sent
  to either client.
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

> The production deploy path is **IBM Cloud Code Engine** — see the section below.

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
| Expenses | GET | `/db/expense/category-suggestion` | ✅ |
| Expenses | GET | `/db/categories` | ❌ |
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
| Settlements | GET | `/db/settlements` | ✅ |
| Settlements | POST | `/db/settlements/:id/confirm` | ✅ |
| Settlements | POST | `/db/settlements/:id/reject` | ✅ |
| PayPal Settlements | POST | `/db/user/expense/friend/:id/settleup/paypal` | ✅ |
| PayPal Settlements | POST | `/db/user/expense/friend/:id/settleup/upi` | ✅ |
| PayPal Settlements | POST | `/db/settlements/paypal/:order_id/capture` | ✅ |
| PayPal Settlements | GET | `/db/settlements/paypal/:order_id/status` | ✅ |
| PayPal Settlements | POST | `/db/webhooks/paypal` | ✅ |
| Activity | GET | `/db/activity` | ✅ |
| Activity | POST | `/db/activity/:id/undo` | ✅ |
| Cards | GET | `/db/cards` | ✅ |
| Cards | POST | `/db/cards` | ✅ |
| Cards | PUT | `/db/cards/:id` | ✅ |
| Cards | DELETE | `/db/cards/:id` | ✅ |
| FX | GET | `/db/fx/currencies` | ❌ |
| FX | GET | `/db/fx/rate` | ✅ |
| Payments | GET | `/db/payment/:link` | ❌ |

### Currency conversion

There is no live FX feed, on purpose: the rate a person is actually charged
includes their card's forex markup, so a mid-market quote would be wrong. Rates
are resolved most-specific-first:

1. an explicit `fxRate` on the expense — what that transaction really cost;
2. the group's `fxRates` table (`{code: rate into the group's currency}`);
3. the user's own `fxRates` table, for friend-to-friend and solo expenses;
4. the `fx_rates` collection, then `FX_API_URL` if one is configured;
5. identity (1.0), with a warning — nothing has been told to us yet.

`GET /db/fx/rate` returns a `source` field naming which of these answered, so
the client can prompt for a rate instead of converting silently.

### Settle-up confirmation

Whoever *owes* money can only claim to have paid it, so `POST .../settleup`
from the debtor creates a pending `settlement_request` and returns
`status: "pending"`; the creditor confirms it via `/db/settlements/:id/confirm`.
When the creditor initiates, there is nobody left to confirm and it completes
immediately (`status: "settled"`). Emails go to **both** parties, and only once
the settlement is recorded — never on a pending request.

### PayPal + UPI settlements (PayPal AI Hackathon entry)

A debt is settled in full, debtor → creditor, over one of two rails, chosen
from the two users' `country` fields (ISO alpha-2, set on the profile):

- **PayPal** — `POST .../settleup/paypal` creates a PayPal Orders v2 order
  with the creditor as `payee` and returns the Checkout approval URL. After
  the debtor approves, `POST /db/settlements/paypal/:order_id/capture`
  captures it; the server then re-fetches the order and only marks the debt
  settled when it is COMPLETED for the exact amount — the client redirect
  is never trusted. `POST /db/webhooks/paypal` is the production-grade path:
  a signature-verified `PAYMENT.CAPTURE.COMPLETED` event settles the debt
  even if the payer closed the browser. Never the Payouts API (business-only,
  and its terms forbid friends-and-family use). The fee estimate is shown
  before confirming (US domestic 3.49% + $0.49; cross-border 4.4% + $0.30
  visible fee, plus ~3–4% FX markup and 18% GST described in the note).
- **UPI** (India → India) — `POST .../settleup/upi` returns a `upi://pay`
  intent link + a QR code (generated locally, no third-party service) and
  records a pending `settlement_request` the creditor confirms with the
  existing confirm endpoint. The payment happens in the payer's own UPI app;
  there is no automatic confirmation (no UPI webhook exists).

Routing: IN → IN = UPI (INR); non-IN → IN = PayPal USD (Indian accounts
receive cross-border only, $10,000/receipt cap); IN → abroad = unavailable
(Indian PayPal cannot send abroad); missing country = unavailable. An INR
debt settles over PayPal in USD only when the payer has an explicit INR→USD
rate on their account — an FX rate is never guessed for money movement. If
the balance changed after the payment was created, the capture is verified
but the debt is flagged `NEEDS_REVIEW` instead of auto-settling the wrong
amount. PayPal credentials come from the environment only
(`PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` / `PAYPAL_ENVIRONMENT` /
`PAYPAL_WEBHOOK_ID` / `PAYPAL_RETURN_URL` / `PAYPAL_CANCEL_URL`); with
them unset the endpoints return 503 and the rest of the app is unaffected.
Offline tests: `pytest tests/offline/test_paypal_settlements.py` (34 tests,
mocked PayPal transport, in-memory fake DB — no live calls, no MongoDB).

### Activity log

Expense, event, and settlement writes append to `audit_log` with a full
before/after document snapshot, visible to everyone the change affected.
`POST /db/activity/:id/undo` restores that snapshot. Undo is refused if the
document changed afterwards (409), if it was already undone, or outside the
7-day window.

### Categories

`category_service.CATEGORIES` is the single category vocabulary — the Flutter
pickers and the receipt-scanner prompt all read from it, and `normalize()` folds
older spellings, Splitwise's names and scanner output onto it on every write.

Suggestions (`GET /db/expense/category-suggestion?name=`) come from three
places, best first:

1. **You** — what this person filed a title like this under before.
2. **Everyone** — the same, aggregated. This is the part that grows: saving or
   correcting an expense increments a tally in `category_hints`, keyed by the
   whole title (weighted ×3) and by each meaningful word in it.
3. **Keywords** — a static seed table, so a fresh install still guesses well.

A correction outvotes the old answer over time rather than needing to erase it,
and settlements never teach the table. No model is involved; it is counting.

### Importing

`POST /db/import/splitwise` creates a group from a Splitwise export. Members
that are not mapped to an account or an email become name-only placeholders, so
the import runs without identifying everyone up front.

Passing `eventId` instead of `eventName` imports into an existing group and adds
**only the rows that are not already there** — each expense stores an
`importKey` fingerprint of its source row, so re-exporting after a trip tops the
group up rather than duplicating it. Rows imported before fingerprints existed
are matched on a looser key (date, description, cost, currency). The preview
endpoint reports `newRowCount` / `duplicateRowCount` up front.

### Credit-card reminders

`credit_cards` stores a name, optional last four digits, and the **day of the
month** a bill falls due. A background loop in the app lifespan sends email
reminders 10 and 5 days ahead. Sends are de-duplicated by a unique index on
`card_reminder_log`, so running on several instances mails nobody twice.

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
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | PayPal REST app credentials (Developer Dashboard; sandbox first) | (empty = PayPal settlements disabled, 503) |
| `PAYPAL_ENVIRONMENT` | `sandbox` or `live` | `sandbox` |
| `PAYPAL_WEBHOOK_ID` | Id of the webhook registered in the PayPal dashboard | (empty) |
| `PAYPAL_RETURN_URL` / `PAYPAL_CANCEL_URL` | Frontend pages PayPal returns the payer to | (empty) |
| `CORS_ORIGINS` | Comma-separated allowed origins (add the hosting domain) | `http://localhost:3000,...` |
| `HOST` | Server host (local only) | `0.0.0.0` |
| `PORT` | Server port (AppSail injects its own listen port) | `8081` |

> In production these live in `catalyst/appsail/app-config.json` `env_variables`
> and the Catalyst console (secrets never enter the bundle or repo). Locally they
> all come from `.env`. See `.env.example` for the full list.

## Deployment (IBM Cloud Code Engine)

Production runs on **IBM Cloud Code Engine** (Chennai, `in-che`) at
`https://splitllm-api.2d1ubdljxfui.in-che.codeengine.appdomain.cloud` — this is
the URL the Flutter client is built against (`API_BASE_URL` repo variable).

### Deploy

```bash
ibmcloud login                 # one-time, plus `ibmcloud plugin install code-engine`
./ibm/deploy.sh                # credit sizing: always warm, no cold starts
./ibm/deploy.sh --free         # free-tier sizing: scales to zero
```

Idempotent — creates the project/secret/app on first run and updates them after.
Code Engine builds the image from `Dockerfile` using a local-source build, so it
deploys your working tree, not the pushed commit. Push first, then deploy.

Env values come from `ibm/secrets.env` (gitignored; see `ibm/secrets.example.env`)
and are synced into the `splitllm-env` Code Engine secret on every run.

Verify: `/health` returns `{"status":"healthy","database":"connected"}` and
`/ready` returns 200 (503 while Mongo is unreachable — readiness gates slow
startup without killing the instance).

### Automated deployment (push to main)

`.github/workflows/deploy.yml` deploys every push to `main` once the tests
pass. The manual script above still works and is the escape hatch.

Two things the workflow deliberately does **not** do:

- **It never runs `app create`.** Creating a Code Engine app establishes its
  build configuration, including the registry secret the built image is pushed
  to, and that needs interactive prompts a CI job cannot answer. The app must
  exist first — created once from a laptop with `./ibm/deploy.sh`. CI only
  updates it.
- **It never sets environment variables.** Those live in the `splitllm-env`
  Code Engine secret, whose source of truth is `ibm/secrets.env`. If CI also
  passed env values, the app's configuration would have two owners that
  silently disagree — and the GitHub copy would drift, because it is the one
  nobody looks at. To change an env value, edit `ibm/secrets.env` and run
  `./ibm/deploy.sh`.

Sizing is likewise owned by `deploy.sh` (`--cpu`, `--memory`, `--min-scale`),
so a CI deploy preserves whatever the last manual run set.

**One-time setup.** The deploy needs an API key belonging to a *service ID*,
not to a person — a personal key carries all of that person's permissions and
stops working when they rotate it or leave.

Everything below lives under **Manage → Access (IAM)** in the IBM Cloud
console. The CLI equivalents are given for reference, but the console is the
easier path since creating the credential requires an interactive login anyway.

1. **Service ID** — *Service IDs → Create*, named `splitllm-ci`.

   ```bash
   ibmcloud iam service-id-create splitllm-ci \
     --description "GitHub Actions deploy for splitllmbill/backend"
   ```

2. **Code Engine access** — on the service ID, *Access → Assign access →
   Code Engine*, scoped to the resource group (or just the `splitllm`
   project). This needs **two** roles, which is easy to get wrong: platform
   `Viewer` to select the project, and service `Writer` to update the app.
   Neither alone is enough, and the resulting failure is a 403 during
   `project select` or `app update` rather than at login.

   ```bash
   ibmcloud iam service-policy-create splitllm-ci \
     --roles Viewer,Writer --service-name codeengine
   ```

3. **Resource group access** — a *separate* policy, and the one most often
   missed: *Assign access → Resource group → Default → Viewer*. Without it
   the workflow cannot target the resource group at login.

   ```bash
   ibmcloud iam service-policy-create splitllm-ci \
     --roles Viewer --resource-group-name Default
   ```

4. **API key** — on the service ID, *API keys → Create*. **Copy it
   immediately**; IBM displays the value once and cannot show it again. If it
   is lost, delete the key and create another.

   ```bash
   ibmcloud iam service-api-key-create splitllm-ci-key splitllm-ci \
     --description "GitHub Actions" --output json
   ```

5. **GitHub** — add that value as the repo secret **`IBMCLOUD_API_KEY`**
   (Settings → Secrets and variables → Actions → New repository secret).

If a deploy fails with a permissions error despite the above, widen the Code
Engine policy to platform `Editor` + service `Manager` to confirm it is a
scoping problem, then narrow it back down.

The region, resource group, project, and app name fall back to the values
`deploy.sh` uses (`in-che` / `Default` / `splitllm` / `splitllm-api`). Override
any of them with repo *variables* `CE_REGION`, `CE_RESOURCE_GROUP`,
`CE_PROJECT`, `CE_APP` — variables, not secrets; none of them are sensitive.

The `deploy` job targets a GitHub **environment** named `production`. Creating
it is optional, but adding a required reviewer there turns every deploy into a
manual approval if you want that gate later.

After deploying, the job polls `/ready` until it answers. `app update --wait`
only reports that the revision came up, which is not the same as it being able
to serve — without the poll, a backend that starts but cannot reach Mongo
reports a green deploy.

## Legacy: Zoho Catalyst AppSail (no longer used)

The API previously ran as an AppSail service in the Catalyst project **SplitLLM**
(`46831000000013050`, org `60078340202`, IN DC). That deployment is dead — the
AppSail URL now returns 404 — and CI no longer deploys to it. The `catalyst/`
directory is kept only for reference:

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

### Platform gotchas (Catalyst — kept for reference)

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
