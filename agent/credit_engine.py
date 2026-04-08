#!/usr/bin/env python3
"""
SolarPunk Mutual Credit Engine
===============================
Issue credit between community members. Balances can go negative
up to a trust-based limit. Settlement cycles rebalance periodically.

No banks. No interest. No credit scores.
Just humans trusting humans.

AGPL-3.0
"""

import json
import datetime
import hashlib
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CREDIT_FILE = DATA_DIR / "credit_accounts.json"
TRUST_FILE = DATA_DIR / "trust_scores.json"
SETTLEMENT_FILE = DATA_DIR / "settlements.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default=None):
    if default is None:
        default = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return default


def _save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    tmp.replace(path)


def _now_iso():
    return datetime.datetime.utcnow().isoformat() + "Z"


def _hash_id(*parts):
    raw = ":".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ===================================================================
# Trust Score
# ===================================================================

class TrustRegistry:
    """
    Community trust scores determine credit limits.

    Score range: 0-100
    - New members start at 10
    - Increases with successful repayments and contributions
    - Decreases with defaults
    - Credit limit = score * 5 (so max = $500 for score 100)
    """

    DEFAULT_SCORE = 10
    SCORE_MIN = 0
    SCORE_MAX = 100
    CREDIT_MULTIPLIER = 5  # credit_limit = score * multiplier

    def __init__(self):
        _ensure_dirs()
        self._data = _load_json(TRUST_FILE, {"scores": {}, "history": []})

    def get_score(self, member: str) -> int:
        return self._data["scores"].get(member, self.DEFAULT_SCORE)

    def credit_limit(self, member: str) -> float:
        """Negative balance floor for this member."""
        return -(self.get_score(member) * self.CREDIT_MULTIPLIER)

    def adjust(self, member: str, delta: int, reason: str = ""):
        """Increase or decrease a member's trust score."""
        current = self.get_score(member)
        new_score = max(self.SCORE_MIN, min(self.SCORE_MAX, current + delta))
        self._data["scores"][member] = new_score
        self._data["history"].append({
            "member": member,
            "old": current,
            "new": new_score,
            "delta": delta,
            "reason": reason,
            "ts": _now_iso(),
        })
        self._save()
        return new_score

    def register(self, member: str) -> int:
        """Register a new community member with default trust score."""
        if member not in self._data["scores"]:
            self._data["scores"][member] = self.DEFAULT_SCORE
            self._data["history"].append({
                "member": member,
                "old": 0,
                "new": self.DEFAULT_SCORE,
                "delta": self.DEFAULT_SCORE,
                "reason": "new_member_registration",
                "ts": _now_iso(),
            })
            self._save()
        return self._data["scores"][member]

    def all_scores(self) -> dict:
        return dict(self._data["scores"])

    def _save(self):
        _save_json(TRUST_FILE, self._data)


# ===================================================================
# Credit Engine
# ===================================================================

class CreditEngine:
    """
    Mutual credit system.

    - Members can issue credit to each other
    - Balances can go negative up to the trust-based limit
    - Settlement cycles periodically rebalance
    - All data persisted to JSON
    """

    def __init__(self):
        _ensure_dirs()
        self._accounts = _load_json(CREDIT_FILE, {
            "accounts": {},
            "credit_lines": [],
            "transactions": [],
        })
        self._trust = TrustRegistry()
        self._settlements = _load_json(SETTLEMENT_FILE, {
            "cycles": [],
            "next_cycle": 1,
        })

    # ---- Member management ----

    def register_member(self, member: str) -> dict:
        """Register a new member in the credit system."""
        self._trust.register(member)
        if member not in self._accounts["accounts"]:
            self._accounts["accounts"][member] = {
                "balance": 0.0,
                "total_issued": 0.0,
                "total_received": 0.0,
                "joined": _now_iso(),
            }
            self._save_accounts()
        return {
            "member": member,
            "balance": self._accounts["accounts"][member]["balance"],
            "credit_limit": self._trust.credit_limit(member),
            "trust_score": self._trust.get_score(member),
        }

    # ---- Credit operations ----

    def issue_credit(self, issuer: str, receiver: str, amount: float,
                     memo: str = "") -> dict:
        """
        Issue credit from one member to another.
        The issuer's balance goes DOWN, receiver's goes UP.
        Issuer must stay above their credit limit.
        """
        if amount <= 0:
            raise ValueError("Amount must be positive")
        if issuer == receiver:
            raise ValueError("Cannot issue credit to yourself")

        # Ensure both are registered
        for m in (issuer, receiver):
            if m not in self._accounts["accounts"]:
                self.register_member(m)

        issuer_acct = self._accounts["accounts"][issuer]
        limit = self._trust.credit_limit(issuer)
        new_balance = issuer_acct["balance"] - amount

        if new_balance < limit:
            raise ValueError(
                f"{issuer} would exceed credit limit ({limit:.2f}). "
                f"Current balance: {issuer_acct['balance']:.2f}, "
                f"trust score: {self._trust.get_score(issuer)}"
            )

        ts = _now_iso()
        tx = {
            "id": _hash_id(issuer, receiver, amount, ts),
            "type": "credit_issue",
            "issuer": issuer,
            "receiver": receiver,
            "amount": amount,
            "memo": memo,
            "ts": ts,
        }

        issuer_acct["balance"] -= amount
        issuer_acct["total_issued"] += amount
        self._accounts["accounts"][receiver]["balance"] += amount
        self._accounts["accounts"][receiver]["total_received"] += amount

        self._accounts["transactions"].append(tx)

        # Track credit line
        self._accounts["credit_lines"].append({
            "id": tx["id"],
            "from": issuer,
            "to": receiver,
            "amount": amount,
            "ts": ts,
        })

        self._save_accounts()
        return tx

    def get_balance(self, member: str) -> dict:
        """Get full balance info for a member."""
        if member not in self._accounts["accounts"]:
            return {
                "member": member,
                "balance": 0.0,
                "credit_limit": self._trust.credit_limit(member),
                "available_credit": abs(self._trust.credit_limit(member)),
                "trust_score": self._trust.get_score(member),
            }
        acct = self._accounts["accounts"][member]
        limit = self._trust.credit_limit(member)
        return {
            "member": member,
            "balance": acct["balance"],
            "credit_limit": limit,
            "available_credit": acct["balance"] - limit,
            "trust_score": self._trust.get_score(member),
            "total_issued": acct["total_issued"],
            "total_received": acct["total_received"],
        }

    # ---- Settlement ----

    def run_settlement_cycle(self) -> dict:
        """
        Settlement cycle: find circular debts and cancel them out.

        Simple algorithm:
        1. Find members with negative balances
        2. Find members with positive balances
        3. Match them and settle smallest amounts first
        4. Record the settlement
        """
        cycle_num = self._settlements["next_cycle"]
        ts = _now_iso()

        debtors = []
        creditors = []
        for member, acct in self._accounts["accounts"].items():
            if acct["balance"] < -0.01:
                debtors.append((member, acct["balance"]))
            elif acct["balance"] > 0.01:
                creditors.append((member, acct["balance"]))

        # Sort: smallest debts first, smallest credits first
        debtors.sort(key=lambda x: x[1])       # most negative first
        creditors.sort(key=lambda x: x[1])      # smallest positive first

        settlements_made = []
        d_idx = 0
        c_idx = 0

        while d_idx < len(debtors) and c_idx < len(creditors):
            debtor, debt = debtors[d_idx]
            creditor, credit = creditors[c_idx]

            settle_amount = min(abs(debt), credit)
            if settle_amount < 0.01:
                break

            # Apply settlement
            self._accounts["accounts"][debtor]["balance"] += settle_amount
            self._accounts["accounts"][creditor]["balance"] -= settle_amount

            settlements_made.append({
                "debtor": debtor,
                "creditor": creditor,
                "amount": round(settle_amount, 2),
            })

            # Update remaining amounts
            debtors[d_idx] = (debtor, debt + settle_amount)
            creditors[c_idx] = (creditor, credit - settle_amount)

            if abs(debtors[d_idx][1]) < 0.01:
                d_idx += 1
            if creditors[c_idx][1] < 0.01:
                c_idx += 1

        cycle = {
            "cycle": cycle_num,
            "ts": ts,
            "settlements": settlements_made,
            "total_settled": sum(s["amount"] for s in settlements_made),
        }

        self._settlements["cycles"].append(cycle)
        self._settlements["next_cycle"] = cycle_num + 1
        self._save_accounts()
        self._save_settlements()

        # Reward good-standing members with trust boost
        for member, acct in self._accounts["accounts"].items():
            if acct["balance"] >= 0:
                self._trust.adjust(member, 1, f"settlement_cycle_{cycle_num}_good_standing")

        return cycle

    # ---- Reporting ----

    def network_summary(self) -> dict:
        """Get a summary of the entire credit network."""
        accounts = self._accounts["accounts"]
        total_positive = sum(a["balance"] for a in accounts.values() if a["balance"] > 0)
        total_negative = sum(a["balance"] for a in accounts.values() if a["balance"] < 0)
        return {
            "total_members": len(accounts),
            "total_transactions": len(self._accounts["transactions"]),
            "total_credit_lines": len(self._accounts["credit_lines"]),
            "total_positive_balance": round(total_positive, 2),
            "total_negative_balance": round(total_negative, 2),
            "net_balance": round(total_positive + total_negative, 2),
            "settlement_cycles": len(self._settlements["cycles"]),
        }

    def member_list(self) -> list:
        """List all members with their balances."""
        result = []
        for member in sorted(self._accounts["accounts"].keys()):
            result.append(self.get_balance(member))
        return result

    def recent_transactions(self, limit: int = 20) -> list:
        return self._accounts["transactions"][-limit:]

    # ---- Persistence ----

    def _save_accounts(self):
        _save_json(CREDIT_FILE, self._accounts)

    def _save_settlements(self):
        _save_json(SETTLEMENT_FILE, self._settlements)


# ===================================================================
# CLI interface
# ===================================================================

def main():
    """Simple CLI for testing the credit engine."""
    import sys

    engine = CreditEngine()

    if len(sys.argv) < 2:
        print("SolarPunk Mutual Credit Engine")
        print("=" * 40)
        print()
        print("Usage:")
        print("  credit_engine.py register <member>")
        print("  credit_engine.py issue <from> <to> <amount> [memo]")
        print("  credit_engine.py balance <member>")
        print("  credit_engine.py settle")
        print("  credit_engine.py summary")
        print("  credit_engine.py members")
        print("  credit_engine.py history [limit]")
        return

    cmd = sys.argv[1]

    if cmd == "register" and len(sys.argv) >= 3:
        result = engine.register_member(sys.argv[2])
        print(json.dumps(result, indent=2))

    elif cmd == "issue" and len(sys.argv) >= 5:
        memo = sys.argv[5] if len(sys.argv) > 5 else ""
        result = engine.issue_credit(
            sys.argv[2], sys.argv[3], float(sys.argv[4]), memo
        )
        print(json.dumps(result, indent=2))

    elif cmd == "balance" and len(sys.argv) >= 3:
        result = engine.get_balance(sys.argv[2])
        print(json.dumps(result, indent=2))

    elif cmd == "settle":
        result = engine.run_settlement_cycle()
        print(json.dumps(result, indent=2))

    elif cmd == "summary":
        result = engine.network_summary()
        print(json.dumps(result, indent=2))

    elif cmd == "members":
        for m in engine.member_list():
            print(f"  {m['member']:20s}  bal={m['balance']:>8.2f}  "
                  f"limit={m['credit_limit']:>8.2f}  trust={m['trust_score']}")

    elif cmd == "history":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        for tx in engine.recent_transactions(limit):
            print(f"  [{tx['ts'][:10]}] {tx['issuer']} -> {tx['receiver']}  "
                  f"${tx['amount']:.2f}  {tx.get('memo', '')}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
