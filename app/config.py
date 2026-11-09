from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):
    # MongoDB
    MONGODB_URI: str = "mongodb://localhost:27017"
    DB_NAME: str = "splitllm"

    # Supabase (for asymmetric JWT validation)
    SUPABASE_URL: str = "https://your-project.supabase.co"
    SUPABASE_ANON_KEY: str = "your-anon-key"
    # Service-role key (secret!) — required for QR/device-code login, and also
    # lets account deletion remove the Supabase auth record.
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # ── PayPal settlements (PayPal AI Hackathon entry) ─────────────────
    # Standard REST app credentials from the PayPal Developer Dashboard
    # (developer.paypal.com). Sandbox first; switch PAYPAL_ENVIRONMENT to
    # "live" only with live credentials. Empty = PayPal settlements are
    # unavailable (the endpoints return 503) — the rest of the app is
    # unaffected. WEBHOOK_ID comes from the webhook registered in the
    # dashboard; RETURN_URL / CANCEL_URL are the frontend pages PayPal
    # sends the payer back to after approval / cancellation.
    PAYPAL_CLIENT_ID: str = ""
    PAYPAL_CLIENT_SECRET: str = ""
    PAYPAL_ENVIRONMENT: str = "sandbox"
    PAYPAL_WEBHOOK_ID: str = ""
    PAYPAL_RETURN_URL: str = ""
    PAYPAL_CANCEL_URL: str = ""

    # LLM feature is disabled by default so the app can deploy and run before the
    # QuickML endpoints are wired up. Set LLM_ENABLED=true once the Catalyst
    # QuickML endpoints below are configured.
    LLM_ENABLED: bool = False

    # Catalyst QuickML LLM Serving (GLM text model + Qwen vision-language)
    # Endpoints come from the QuickML console (LLM Serving tab -> Model Details).
    # Auth uses a Zoho Self Client (client id/secret + refresh token).
    # AppSail rejects env vars named with reserved (ZOHO*/CATALYST*) prefixes,
    # so each field also accepts a QUICKML_/OAUTH_-prefixed env name.
    CATALYST_LLM_ENDPOINT: str = Field(
        "", validation_alias=AliasChoices("QUICKML_LLM_ENDPOINT", "CATALYST_LLM_ENDPOINT")
    )
    CATALYST_LLM_MODEL: str = Field(
        "glm-4.7-flash",
        validation_alias=AliasChoices("QUICKML_LLM_MODEL", "CATALYST_LLM_MODEL"),
    )
    CATALYST_VLM_ENDPOINT: str = Field(
        "", validation_alias=AliasChoices("QUICKML_VLM_ENDPOINT", "CATALYST_VLM_ENDPOINT")
    )
    CATALYST_VLM_MODEL: str = Field(
        "qwen3.6-35b-vl",
        validation_alias=AliasChoices("QUICKML_VLM_MODEL", "CATALYST_VLM_MODEL"),
    )
    CATALYST_ORG_ID: str = Field(
        "", validation_alias=AliasChoices("QUICKML_ORG_ID", "CATALYST_ORG_ID")
    )
    ZOHO_ACCOUNTS_URL: str = Field(
        "https://accounts.zoho.in",
        validation_alias=AliasChoices("OAUTH_ACCOUNTS_URL", "ZOHO_ACCOUNTS_URL"),
    )
    ZOHO_CLIENT_ID: str = Field(
        "", validation_alias=AliasChoices("OAUTH_CLIENT_ID", "ZOHO_CLIENT_ID")
    )
    ZOHO_CLIENT_SECRET: str = Field(
        "", validation_alias=AliasChoices("OAUTH_CLIENT_SECRET", "ZOHO_CLIENT_SECRET")
    )
    ZOHO_REFRESH_TOKEN: str = Field(
        "", validation_alias=AliasChoices("OAUTH_REFRESH_TOKEN", "ZOHO_REFRESH_TOKEN")
    )

    # Currency / FX
    DEFAULT_CURRENCY: str = "INR"
    # External FX endpoint (user-managed: historic backfill + twice-daily live
    # rates). Leave empty until it exists; see fx_service for the contract.
    FX_API_URL: str = ""

    # ZeptoMail (transactional email)
    ZEPTOMAIL_ENABLED: bool = False
    # India DC: https://api.zeptomail.in ; global: https://api.zeptomail.com
    ZEPTOMAIL_API_URL: str = "https://api.zeptomail.in"
    ZEPTOMAIL_TOKEN: str = ""
    ZEPTOMAIL_FROM_ADDRESS: str = "noreply@splitllm.com"
    ZEPTOMAIL_FROM_NAME: str = "SplitLLM"

    # Zoho Mail (SMTP alternative to ZeptoMail)
    ZOHO_MAIL_PASSWORD: str = ""

    # Public URL of the web app (used in email links)
    APP_BASE_URL: str = "https://splitllm.com"

    # ─── App version / update prompt ────────────────────────────────────
    # Defaults for GET /version. A Mongo `app_config` doc (_id="app_version")
    # overrides these at runtime so the version can be bumped without a
    # redeploy (see the admin PUT /db/admin/app-version endpoint).
    APP_LATEST_VERSION: str = "1.0.0"
    APP_LATEST_BUILD: int = 1
    # Clients older than this are forced to update (hard gate).
    APP_MIN_SUPPORTED_VERSION: str = "0.0.0"
    # Where each platform sends users to get the new build.
    APP_ANDROID_URL: str = ""  # Play listing or hosted APK
    APP_IOS_URL: str = ""  # App Store / TestFlight
    APP_UPDATE_NOTES: str = ""

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:8080,http://localhost:5000,http://localhost:8081,http://localhost:52345,http://localhost:52346,http://localhost:52347,http://localhost:52348,http://localhost:52349,http://localhost:52350"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8081

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
