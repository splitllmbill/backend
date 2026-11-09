import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import (
    connect_to_mongo,
    close_mongo_connection,
    get_db,
    ping_database,
)

from app.routers import (
    activity_router,
    auth_router,
    card_router,
    user_router,
    dashboard_router,
    event_router,
    expense_router,
    friend_router,
    context_router,
    payment_router,
    llm_router,
    admin_router,
    issues_router,
    fx_router,
    import_router,
    version_router,
)
from app.routers import recurring_router
from app.routers import settings_router
from app.routers import settlement_router


#: How often the reminder loop wakes. Hourly rather than daily so a container
#: that restarts (Code Engine recycles instances freely) still checks in soon
#: after, instead of waiting up to a day. Sends are de-duplicated in the
#: database, so waking often costs a query and mails nobody twice.
_REMINDER_INTERVAL_SECONDS = 60 * 60


async def _reminder_loop():
    """Post what has come due: card reminders, and recurring expenses."""
    from app.services import card_service, recurring_service

    while True:
        try:
            await card_service.send_due_reminders(get_db())
            await card_service.purge_old_reminder_logs(get_db())
            # Subscriptions that have come due. Same safety properties as the
            # reminders above: de-duplicated in the database, so waking often
            # costs a query and charges nobody twice.
            await recurring_service.post_due(get_db())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A failed pass must not kill the loop — the next one retries, and
            # unsent reminders are still unclaimed.
            logging.getLogger(__name__).error("Reminder pass failed: %s", e)
        await asyncio.sleep(_REMINDER_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle events."""
    # Startup
    await connect_to_mongo()
    # Pre-warm the site-settings cache so sync readers have values immediately.
    try:
        from app.services.site_settings import get_site_settings
        await get_site_settings(get_db())
    except Exception:
        pass  # DB may not be ready; cache will populate on first request.
    reminders = asyncio.create_task(_reminder_loop())
    print("SplitLLM Backend started")
    yield
    # Shutdown
    reminders.cancel()
    try:
        await reminders
    except asyncio.CancelledError:
        pass
    await close_mongo_connection()
    print("SplitLLM Backend stopped")


app = FastAPI(
    title="SplitLLM API",
    description="Backend API for SplitLLM expense-splitting app",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Browsers hide every non-simple response header from JS unless it is
    # named here, and the web build reads this one to tell a pending account
    # apart from a deactivated one.
    expose_headers=["X-Account-Status"],
)


# ─── Cache Control ──────────────────────────────────────────────────────
@app.middleware("http")
async def add_cache_control(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# ─── Register Routers ───────────────────────────────────────────────────

# Auth routes: /db/login, /db/signup, /db/logout, /db/changePassword
app.include_router(auth_router.router, prefix="/db", tags=["Authentication"])

# User routes: /db/user/account
app.include_router(user_router.router, prefix="/db", tags=["User Account"])

# Dashboard routes: /db/dashboard/summary, /db/dashboard/chart
app.include_router(dashboard_router.router, prefix="/db", tags=["Dashboard"])

# Event routes: /db/user/events, /db/event/*, /db/user/event/*/dues
app.include_router(event_router.router, prefix="/db", tags=["Events"])

# Expense routes: /db/expense/*, /db/expenses/personal
app.include_router(expense_router.router, prefix="/db", tags=["Expenses"])

# Friend routes: /db/user/friends, /db/addFriend, /db/deleteFriend, etc.
app.include_router(friend_router.router, prefix="/db", tags=["Friends"])

# Context routes: /db/:type/:contextId/users
app.include_router(context_router.router, prefix="/db", tags=["Context Users"])

# Payment routes: /db/payment, /db/payment/:link
app.include_router(payment_router.router, prefix="/db", tags=["Payments"])

# LLM routes: /llm/expense
app.include_router(llm_router.router, prefix="/llm", tags=["LLM"])

# Admin routes: /db/admin/*
app.include_router(admin_router.router, prefix="/db/admin", tags=["Admin"])

# Issues routes: /db/issues
app.include_router(issues_router.router, prefix="/db/issues", tags=["Issues"])

# FX routes: /db/fx/currencies, /db/fx/rate
app.include_router(fx_router.router, prefix="/db", tags=["FX"])

# Import routes: /db/import/splitwise, /db/import/splitwise/preview
app.include_router(import_router.router, prefix="/db/import", tags=["Import"])

# Activity routes: /db/activity, /db/activity/:id/undo
app.include_router(activity_router.router, prefix="/db", tags=["Activity"])

# Credit-card bill reminder routes: /db/cards
app.include_router(card_router.router, prefix="/db", tags=["Cards"])

# Subscriptions and other repeating expenses
app.include_router(recurring_router.router, prefix="/db", tags=["Recurring"])

# Version route: /version (public; drives the in-app update prompt)
app.include_router(version_router.router, tags=["Version"])

# Public site settings: /db/settings/public (unauthenticated)
app.include_router(settings_router.router, prefix="/db", tags=["Settings"])

# PayPal + UPI settlements (PayPal AI Hackathon entry). The settleup
# endpoints live under /db next to the existing settle-up flow; the webhook
# is unauthenticated by design (PayPal signature verification is the auth).
app.include_router(settlement_router.router, prefix="/db", tags=["PayPal Settlements"])


# ─── Health Check ───────────────────────────────────────────────────────
@app.get("/", tags=["Health"])
async def root():
    return {"message": "SplitLLM API is running", "version": "2.0.0"}


@app.get("/health", tags=["Health"])
async def health_check():
    """Liveness: is this process alive?

    Always 200 while the process can serve, and reports database state in the
    body for observability. It deliberately does NOT fail on an unreachable
    database: the liveness probe restarts the container on failure, and
    restarting an app cannot fix a Mongo outage — it would only turn a
    dependency failure into a cluster-wide restart loop. Use /ready to gate
    traffic on the database.
    """
    # Short timeout: this response must land well inside the liveness probe's
    # timeout even when Mongo is hanging, or the probe kills the container.
    db_ok, db_reason = await ping_database(timeout_s=1.0)
    body = {
        "status": "healthy",
        "database": "connected" if db_ok else "unreachable",
    }
    if db_reason:
        body["databaseReason"] = db_reason
    return body


@app.get("/ready", tags=["Health"])
async def readiness_check(response: Response):
    """Readiness: can this instance actually serve requests?

    503 when Mongo is unreachable, which takes the instance out of the load
    balancer without killing it — it rejoins on its own once the database is
    back. This is what the Code Engine readiness probe targets.
    """
    db_ok, db_reason = await ping_database(timeout_s=3.0)
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not ready", "database": "unreachable", "databaseReason": db_reason}
    return {"status": "ready", "database": "connected"}
