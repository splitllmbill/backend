from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ─── Auth ───────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str = ""
    # Carried through so a user whose signup call failed (e.g. email-confirmation
    # delayed the session) is still created with their invite code on first login.
    inviteCode: str = ""


class SignupRequest(BaseModel):
    email: str
    name: str
    password: str = ""
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
    password: str


# ─── Account ────────────────────────────────────────────────────────────

class UpdateAccountRequest(BaseModel):
    name: Optional[str] = None
    upiId: Optional[str] = None
    defaultCurrency: Optional[str] = None


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

class EventCreate(BaseModel):
    title: Optional[str] = Field(None, alias="eventName")
    description: str = ""
    members: list[str] = Field(default_factory=list, alias="users")
    currency: Optional[str] = None

    class Config:
        populate_by_name = True


class EventUpdate(BaseModel):
    id: str
    title: Optional[str] = Field(None, alias="eventName")
    description: str = ""
    members: list[str] = Field(default_factory=list, alias="users")
    currency: Optional[str] = None
    # Only meaningful when the currency changes on a group that already has
    # expenses: True re-converts them all, False leaves them as recorded.
    # Omitted (None) makes the API ask rather than guess.
    applyToExpenses: Optional[bool] = None

    class Config:
        populate_by_name = True


class MemberRatesRequest(BaseModel):
    """A member's own conversion rates into the group currency.

    Rates replace that member's previous overrides wholesale; a currency left
    out falls back to the market rate.
    """

    rates: dict[str, float] = Field(default_factory=dict)


class JoinEventRequest(BaseModel):
    # Take over an existing (placeholder) member's spot instead of joining as
    # a new person, so their history becomes yours.
    claimMemberId: Optional[str] = None


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
