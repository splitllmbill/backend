from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from jwt import PyJWKClient
from app.config import settings
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import get_db
from app.services import user_service

security = HTTPBearer()

# Configure the JWKS client to fetch Supabase public keys for asymmetric verification
jwks_url = f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"
jwks_client = PyJWKClient(
    jwks_url,
    # The .well-known endpoint is fully public and doesn't require an apikey header
    cache_keys=True
)

async def get_verified_identity(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Verify a Supabase JWT (asymmetric JWKS) and return its identity WITHOUT
    touching MongoDB. Signup/login depend on this: get_current_user would try
    to auto-create the user before the route can supply the invite code, which
    rejected every new (non-first) user with "Invite code is required".
    """
    token = credentials.credentials
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        decoded_token = jwt.decode(
            token,
            signing_key.key,
            # Pin to the asymmetric algorithms Supabase's JWKS issues. Allowing
            # HS256 alongside a JWKS public key invites an algorithm-confusion
            # attack (a token HMAC-signed with the public key).
            algorithms=["RS256", "ES256"],
            audience="authenticated"
        )

        uid = decoded_token.get("sub")
        email = decoded_token.get("email", "")
        user_metadata = decoded_token.get("user_metadata", {})
        name = user_metadata.get("full_name", user_metadata.get("name", ""))

        if not uid:
            raise HTTPException(status_code=401, detail="Invalid token: missing subject (sub)")

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Expired authentication token")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid authentication token: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")

    return {"uid": uid, "email": email, "name": name}


async def get_account_user(
    # Declared as a dependency rather than called inline so it sits in
    # FastAPI's graph, where it can be overridden (tests) and cached across
    # the several dependencies of one request.
    identity: dict = Depends(get_verified_identity),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    """Verified identity plus its MongoDB account, with no status check.

    For the handful of routes that must keep working while an account is
    gated: reading your own profile (so the app can tell you *why* you are
    stuck), redeeming an invite code, deleting the account, and signing out.
    Everything else wants get_current_user.
    """
    # Sync user to MongoDB
    user = await user_service.find_or_create_user(
        db, uid=identity["uid"], email=identity["email"], name=identity["name"]
    )

    return {
        "uid": identity["uid"],
        "email": identity["email"],
        "name": identity["name"],
        "role": user.get("role", "user"),
        "status": user_service.account_status(user),
    }


# Why each gated status is refused, in the words the app shows the person.
_BLOCKED_REASON = {
    user_service.STATUS_PENDING: (
        "This account needs an invite code before it can be used."
    ),
    user_service.STATUS_INACTIVE: (
        "This account has been deactivated. Contact an administrator."
    ),
}


async def get_current_user(
    user: dict = Depends(get_account_user),
) -> dict:
    """
    FastAPI dependency that verifies a Supabase JWT token using asymmetric JWKS.
    Also ensures the user exists in the local MongoDB.
    Returns a dict with 'uid' and 'email'.

    Anything other than an active account is refused here, which is what makes
    "inactive" mean something: every route that takes this dependency — which
    is every route that touches data — is closed to them at once.
    """
    status = user.get("status", user_service.STATUS_ACTIVE)
    if status != user_service.STATUS_ACTIVE:
        raise HTTPException(
            status_code=403,
            detail=_BLOCKED_REASON.get(status, "This account is not active."),
            # Lets the app route straight to the unblock screen instead of
            # pattern-matching on prose.
            headers={"X-Account-Status": status},
        )
    return user

async def get_admin_user(
    current_user: dict = Depends(get_current_user)
) -> dict:
    """Dependency that requires the current user to be an admin."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user
