#!/usr/bin/env python3
"""
SolarPunk Community Bank Agent
==============================
Mutual credit ledger, micro-loans, community savings, Bitcoin tips.
No credit score. No interest. No gatekeepers.

AGPL-3.0 -- built for the people who got left behind.
"""

import json
import os
import sys
import hashlib
import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LEDGER_FILE = DATA_DIR / "ledger.json"
LOANS_FILE = DATA_DIR / "loans.json"
SAVINGS_FILE = DATA_DIR / "savings.json"
REPORTS_DIR = DATA_DIR / "reports"

# Bitcoin tip jar -- read from env or use placeholder
BTC_ADDRESS = os.environ.get(
    "WALLET_BTC_SEGWIT",
    "bc1q_PLACEHOLDER_replace_with_real_segwit_address"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_LOAN_AMOUNT = 50.00        # dollars -- zero interest
LOAN_TERM_DAYS = 30            # repayment window
DEFAULT_CREDIT_LIMIT = -100.00 # mutual credit floor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_dirs():
    """Create data directories if they don't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default=None):
    """Load a JSON file; return *default* if it doesn't exist yet."""
    if default is None:
        default = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return default


def _save_json(path: Path, data):
    """Atomically write JSON (write-tmp then rename)."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    tmp.replace(path)


def _now_iso():
    return datetime.datetime.utcnow().isoformat() + "Z"


def _tx_id(sender, receiver, amount, ts):
    raw = f"{sender}:{receiver}:{amount}:{ts}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ===================================================================
# Ledger -- mutual credit balances
# ===================================================================

class Ledger:
    """JSON-backed mutual credit ledger."""

    def __init__(self):
        _ensure_dirs()
        self._data = _load_json(LEDGER_FILE, {"balances": {}, "transactions": []})

    # ---- public API ----

    def balance(self, member: str) -> float:
        return self._data["balances"].get(member, 0.0)

    def transfer(self, sender: str, receiver: str, amount: float,
                 memo: str = "") -> dict:
        """Move *amount* from sender to receiver (mutual credit -- can go negative)."""
        if amount <= 0:
            raise ValueError("Amount must be positive")
        sender_bal = self.balance(sender) - amount
        if sender_bal < DEFAULT_CREDIT_LIMIT:
            raise ValueError(
                f"{sender} would exceed credit limit "
                f"({DEFAULT_CREDIT_LIMIT}). Current balance: {self.balance(sender):.2f}"
            )
        ts = _now_iso()
        tx = {
            "id": _tx_id(sender, receiver, amount, ts),
            "ts": ts,
            "sender": sender,
            "receiver": receiver,
            "amount": amount,
            "memo": memo,
        }
        self._data["balances"].setdefault(sender, 0.0)
        self._data["balances"].setdefault(receiver, 0.0)
        self._data["balances"][sender] -= amount
        self._data["balances"][receiver] += amount
        self._data["transactions"].append(tx)
        self.save()
        return tx

    def members(self):
        return list(self._data["balances"].keys())

    def transactions(self, limit: int = 50):
        return self._data["transactions"][-limit:]

    def save(self):
        _save_json(LEDGER_FILE, self._data)


# ===================================================================
# Micro-Loans -- zero interest, max $50, 30-day term
# ===================================================================

class LoanBook:
    """Zero-interest emergency micro-loan tracker."""

    def __init__(self):
        _ensure_dirs()
        self._data = _load_json(LOANS_FILE, {"active": [], "repaid": [], "defaulted": []})

    def issue(self, borrower: str, amount: float) -> dict:
        if amount <= 0 or amount > MAX_LOAN_AMOUNT:
            raise ValueError(f"Loan must be between $0.01 and ${MAX_LOAN_AMOUNT:.2f}")
        # one active loan per person
        for loan in self._data["active"]:
            if loan["borrower"] == borrower:
                raise ValueError(f"{borrower} already has an active loan")
        ts = _now_iso()
        due_dt = datetime.datetime.utcnow() + datetime.timedelta(days=LOAN_TERM_DAYS)
        loan = {
            "id": _tx_id("BANK", borrower, amount, ts),
            "borrower": borrower,
            "amount": amount,
            "issued": ts,
            "due": due_dt.isoformat() + "Z",
            "status": "active",
        }
        self._data["active"].append(loan)
        self._save()
        return loan

    def repay(self, borrower: str, amount: float) -> dict:
        for i, loan in enumerate(self._data["active"]):
            if loan["borrower"] == borrower:
                if amount < loan["amount"]:
                    loan["amount"] -= amount
                    self._save()
                    return {"status": "partial", "remaining": loan["amount"]}
                loan["status"] = "repaid"
                loan["repaid_at"] = _now_iso()
                self._data["repaid"].append(self._data["active"].pop(i))
                self._save()
                return {"status": "repaid"}
        raise ValueError(f"No active loan for {borrower}")

    def check_defaults(self):
        """Move overdue loans to defaulted list (no penalty -- just tracking)."""
        now = datetime.datetime.utcnow()
        still_active = []
        for loan in self._data["active"]:
            due = datetime.datetime.fromisoformat(loan["due"].rstrip("Z"))
            if now > due:
                loan["status"] = "defaulted"
                loan["defaulted_at"] = _now_iso()
                self._data["defaulted"].append(loan)
            else:
                still_active.append(loan)
        self._data["active"] = still_active
        self._save()

    def summary(self):
        return {
            "active": len(self._data["active"]),
            "repaid": len(self._data["repaid"]),
            "defaulted": len(self._data["defaulted"]),
            "total_outstanding": sum(ln["amount"] for ln in self._data["active"]),
        }

    def _save(self):
        _save_json(LOANS_FILE, self._data)


# ===================================================================
# Community Savings Pool
# ===================================================================

class SavingsPool:
    """Track community savings contributions."""

    def __init__(self):
        _ensure_dirs()
        self._data = _load_json(
            SAVINGS_FILE, {"pool_balance": 0.0, "contributions": []}
        )

    def contribute(self, member: str, amount: float) -> dict:
        if amount <= 0:
            raise ValueError("Contribution must be positive")
        ts = _now_iso()
        entry = {"member": member, "amount": amount, "ts": ts}
        self._data["contributions"].append(entry)
        self._data["pool_balance"] += amount
        self._save()
        return entry

    @property
    def balance(self):
        return self._data["pool_balance"]

    def top_contributors(self, n: int = 10):
        totals = {}
        for c in self._data["contributions"]:
            totals[c["member"]] = totals.get(c["member"], 0.0) + c["amount"]
        ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)
        return ranked[:n]

    def _save(self):
        _save_json(SAVINGS_FILE, self._data)


# ===================================================================
# Daily Report
# ===================================================================

def generate_daily_report() -> str:
    """Build a plain-text daily financial summary."""
    ledger = Ledger()
    loans = LoanBook()
    savings = SavingsPool()

    loans.check_defaults()

    lines = [
        "=" * 60,
        "  SOLARPUNK COMMUNITY BANK -- DAILY REPORT",
        f"  {_now_iso()}",
        "=" * 60,
        "",
        "[MUTUAL CREDIT LEDGER]",
        f"  Members:          {len(ledger.members())}",
        f"  Recent txns:      {len(ledger.transactions(50))}",
    ]

    balances = {m: ledger.balance(m) for m in ledger.members()}
    if balances:
        lines.append(f"  Highest balance:  {max(balances.values()):.2f}")
        lines.append(f"  Lowest balance:   {min(balances.values()):.2f}")

    ls = loans.summary()
    lines += [
        "",
        "[MICRO-LOANS]",
        f"  Active loans:     {ls['active']}",
        f"  Total outstanding:${ls['total_outstanding']:.2f}",
        f"  Repaid:           {ls['repaid']}",
        f"  Defaulted:        {ls['defaulted']}",
    ]

    lines += [
        "",
        "[COMMUNITY SAVINGS]",
        f"  Pool balance:     ${savings.balance:.2f}",
        f"  Top contributors: {savings.top_contributors(5)}",
    ]

    lines += [
        "",
        "[BITCOIN TIP JAR]",
        f"  Address: {BTC_ADDRESS}",
        "",
        "=" * 60,
    ]

    report = "\n".join(lines)

    # Save report to file
    report_path = REPORTS_DIR / f"report_{datetime.date.today().isoformat()}.txt"
    report_path.write_text(report, encoding="utf-8")

    return report


# ===================================================================
# Main
# ===================================================================

def run():
    """Entry point -- run the daily cycle."""
    print("[SolarPunk Bank] Starting daily cycle...")

    ledger = Ledger()
    loans = LoanBook()
    savings = SavingsPool()

    # Check for defaulted loans
    loans.check_defaults()

    # Generate daily report
    report = generate_daily_report()
    print(report)

    print("\n[SolarPunk Bank] Daily cycle complete.")
    return report


if __name__ == "__main__":
    run()
