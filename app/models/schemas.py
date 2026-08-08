from pydantic import BaseModel, Field
from typing import Optional, Union
from datetime import datetime


# ─── Auth ───────────────────────────────────────────────────────────────

# NB: these models deliberately have no `password` field. Clients still send
# one; pydantic ignores unknown keys, and the backend stores no password —
# Supabase is the sole authenticator.

class LoginRequest(BaseModel):
    # Optional: the handler prefers the email on the verified JWT. Clients that
    # register after an OAuth/email-confirmation redirect have a session but no
    # form fields to send, and a required email made those calls 422.
    email: str = ""
    # Carried through so a user whose signup call failed (e.g. email-confirmation
    # delayed the session) is still created with their invite code on first login.
    inviteCode: str = ""


class SignupRequest(BaseModel):
    email: str
    name: str
    inviteCode: str = ""


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    friendCode: str
    upiId: str = ""
    role: str = "user"
    invitedBy: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    password: str = ""


class ActivateRequest(BaseModel):
    """Invite code redeemed by an account that signed up without one."""

    inviteCode: str = ""


# ─── Account ────────────────────────────────────────────────────────────

class UpdateAccountRequest(BaseModel):
    name: Optional[str] = None
    upiId: Optional[str] = None
    defaultCurrency: Optional[str] = None
    # Personal FX table: {currency code: rate into defaultCurrency}. Used for
    # friend-to-friend and solo expenses, which have no group to inherit from.
    fxRates: Optional[dict[str, float]] = None


# ─── Dashboard ──────────────────────────────────────────────────────────

class DashboardRequest(BaseModel):
    startDate: Optional[str] = None
    endDate: Optional[str] = None


# ─── Issues ─────────────────────────────────────────────────────────────

class IssueCreate(BaseModel):
    type: str = "issue" # "issue" or "feature_request"
    title: str
    description: str

class IssueResponse(BaseModel):
    id: str
    userId: str
    type: str
    title: str
    description: str
    status: str
    createdAt: str


# ─── Admin ──────────────────────────────────────────────────────────────

class AdminModelUpdate(BaseModel):
    activeModel: str


# ─── Events ─────────────────────────────────────────────────────────────

#: A group member as sent by the client: an existing user's id, an email
#: address, or ``{"name": ..., "email": ...}`` for someone being added by name
#: (the email is optional — without one they are an uninvited placeholder).
#: See event_service._resolve_users.
MemberRef = Union[str, dict]


class EventCreate(BaseModel):
    title: Optional[str] = Field(None, alias="eventName")
    description: str = ""
    members: list[MemberRef] = Field(default_factory=list, alias="users")
    currency: Optional[str] = None
    # {currency code: rate into this event's currency}, set by the members.
    fxRates: Optional[dict[str, float]] = None
    # Net everyone's balance down to the fewest transfers (Splitwise's
    # "simplify debts") instead of settling each pair separately.
    simplifyDebts: Optional[bool] = None

    class Config:
        populate_by_name = True


class EventUpdate(BaseModel):
    id: str
    title: Optional[str] = Field(None, alias="eventName")
    description: str = ""
    members: list[MemberRef] = Field(default_factory=list, alias="users")
    currency: Optional[str] = None
    fxRates: Optional[dict[str, float]] = None
    simplifyDebts: Optional[bool] = None

    class Config:
        populate_by_name = True


class JoinGroupRequest(BaseModel):
    #: Id of a placeholder member the joiner is identifying themselves as.
    claimMemberId: str = ""


# ─── Shares ─────────────────────────────────────────────────────────────

class ShareModel(BaseModel):
    userId: str
    amount: float
    percentage: Optional[float] = None
    userName: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    eventId: Optional[str] = None


class PayShareModel(BaseModel):
    userId: str
    amount: float
    name: Optional[str] = None
    email: Optional[str] = None


# ─── Expenses ───────────────────────────────────────────────────────────

class ExpenseCreate(BaseModel):
    expenseName: str = ""
    title: Optional[str] = None  # alias for expenseName
    amount: float
    category: str = "General"
    description: str = ""
    paidBy: Optional[str] = None
    eventId: Optional[str] = None
    friendId: Optional[str] = None
    type: str = "normal"
    date: str = ""
    currency: Optional[str] = None  # transaction currency; defaults to context currency
    # What this transaction actually cost in the context currency, per unit of
    # `currency` — card markup included. Beats every stored rate table.
    fxRate: Optional[float] = None
    shares: list[ShareModel] = Field(default_factory=list)
    payShares: list[PayShareModel] = Field(default_factory=list)

    def get_expense_name(self) -> str:
        return self.title or self.expenseName or "Untitled"


class ExpenseUpdate(BaseModel):
    expenseName: str = ""
    title: Optional[str] = None
    amount: float = 0
    category: str = ""
    description: str = ""
    paidBy: Optional[str] = None
    eventId: Optional[str] = None
    type: Optional[str] = None
    date: Optional[str] = None
    currency: Optional[str] = None
    fxRate: Optional[float] = None
    shares: list[ShareModel] = Field(default_factory=list)
    payShares: list[PayShareModel] = Field(default_factory=list)

    def get_expense_name(self) -> str:
        return self.title or self.expenseName or ""


# ─── Personal Expenses ──────────────────────────────────────────────────

class PersonalExpensesRequest(BaseModel):
    filters: list[dict] = Field(default_factory=list)


# ─── Friends ────────────────────────────────────────────────────────────

class FriendAddRequest(BaseModel):
    friendCode: str


class FriendDeleteRequest(BaseModel):
    friendCode: str


# ─── Payment ────────────────────────────────────────────────────────────

class PaymentCreateRequest(BaseModel):
    destination: str
    amount: float
    note: str
    expiry: int = 24  # hours


# ─── LLM ────────────────────────────────────────────────────────────────

class LLMExpenseRequest(BaseModel):
    requestData: dict


# ─── UPI ────────────────────────────────────────────────────────────────

class UPILinkRequest(BaseModel):
    destination: str
    amount: float
    note: str


# ─── Dashboard Filter Options ──────────────────────────────────────────

class FilterOptionsRequest(BaseModel):
    model: str = "expense"


# ─── Bulk expense import ────────────────────────────────────────────────

class BulkExpenseRequest(BaseModel):
    """A batch of expenses from any producer — a chatbot, or a parsed statement.

    ``payload`` takes either the parsed object or the raw text a chatbot
    returned; the service copes with a ```json fence either way.
    """

    payload: object = None
    eventId: Optional[str] = None
