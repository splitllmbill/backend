from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import connect_to_mongo, close_mongo_connection

from app.routers import (
    auth_router,
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
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle events."""
    # Startup
    await connect_to_mongo()
    print("SplitLLM Backend started")
    yield
    # Shutdown
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


# ─── Health Check ───────────────────────────────────────────────────────
@app.get("/", tags=["Health"])
async def root():
    return {"message": "SplitLLM API is running", "version": "2.0.0"}


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}
