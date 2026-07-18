"""
oos_loans_db.py – Simple debt notes for oos (ledger only).

Does not hold or transfer currency — just who owes whom.

Tables
------
  oos_loans – one row per open or settled debt note
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("bot.oos_loans_db")

_DATABASE_URL: str = os.environ.get("DATABASE_URL", "")


@contextmanager
def _connect():
    if not _DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    con = psycopg2.connect(_DATABASE_URL, sslmode="require")
    try:
        yield con
    finally:
        con.close()


def init_tables() -> None:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS oos_loans (
                    id           BIGSERIAL PRIMARY KEY,
                    guild_id     BIGINT NOT NULL,
                    lender_id    BIGINT NOT NULL,
                    borrower_id  BIGINT NOT NULL,
                    amount       BIGINT NOT NULL CHECK (amount > 0),
                    note         TEXT,
                    created_by   BIGINT NOT NULL,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    paid_at      TIMESTAMPTZ,
                    CHECK (lender_id <> borrower_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS oos_loans_guild_active_idx
                ON oos_loans (guild_id)
                WHERE paid_at IS NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS oos_loans_guild_lender_idx
                ON oos_loans (guild_id, lender_id)
                WHERE paid_at IS NULL
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS oos_loans_guild_borrower_idx
                ON oos_loans (guild_id, borrower_id)
                WHERE paid_at IS NULL
                """
            )
        con.commit()


try:
    init_tables()
except Exception as exc:
    log.error("Failed to initialise oos_loans tables: %s", exc)


def add_loan(
    guild_id: int,
    lender_id: int,
    borrower_id: int,
    amount: int,
    created_by: int,
    note: Optional[str] = None,
) -> dict[str, Any]:
    if amount <= 0:
        raise ValueError("amount must be positive")
    if lender_id == borrower_id:
        raise ValueError("lender and borrower must differ")
    clean_note = (note or "").strip() or None
    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO oos_loans
                    (guild_id, lender_id, borrower_id, amount, note, created_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (guild_id, lender_id, borrower_id, amount, clean_note, created_by),
            )
            row = dict(cur.fetchone())
        con.commit()
    return row


def get_loan(guild_id: int, loan_id: int) -> Optional[dict[str, Any]]:
    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM oos_loans
                WHERE guild_id = %s AND id = %s
                """,
                (guild_id, loan_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def list_active_for_user(guild_id: int, user_id: int) -> list[dict[str, Any]]:
    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM oos_loans
                WHERE guild_id = %s
                  AND paid_at IS NULL
                  AND (lender_id = %s OR borrower_id = %s)
                ORDER BY created_at ASC, id ASC
                """,
                (guild_id, user_id, user_id),
            )
            return [dict(r) for r in cur.fetchall()]


def list_active_involving(
    guild_id: int, user_a: int, user_b: int
) -> list[dict[str, Any]]:
    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM oos_loans
                WHERE guild_id = %s
                  AND paid_at IS NULL
                  AND (
                        (lender_id = %s AND borrower_id = %s)
                     OR (lender_id = %s AND borrower_id = %s)
                  )
                ORDER BY created_at ASC, id ASC
                """,
                (guild_id, user_a, user_b, user_b, user_a),
            )
            return [dict(r) for r in cur.fetchall()]


def summarize_user(guild_id: int, user_id: int) -> dict[str, int]:
    """Totals for active loans: owed_by_me, owed_to_me, net (owed_to_me - owed_by_me)."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN borrower_id = %s THEN amount ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN lender_id = %s THEN amount ELSE 0 END), 0)
                FROM oos_loans
                WHERE guild_id = %s AND paid_at IS NULL
                  AND (lender_id = %s OR borrower_id = %s)
                """,
                (user_id, user_id, guild_id, user_id, user_id),
            )
            owed_by_me, owed_to_me = cur.fetchone()
    owed_by_me = int(owed_by_me)
    owed_to_me = int(owed_to_me)
    return {
        "owed_by_me": owed_by_me,
        "owed_to_me": owed_to_me,
        "net": owed_to_me - owed_by_me,
    }


def pay_loan(guild_id: int, loan_id: int, amount: Optional[int] = None) -> dict[str, Any]:
    """
    Apply a payment. If amount is None or >= remaining, mark fully paid.
    Returns updated row (+ keys: paid_fully, paid_amount).
    """
    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM oos_loans
                WHERE guild_id = %s AND id = %s
                FOR UPDATE
                """,
                (guild_id, loan_id),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError("loan not found")
            if row["paid_at"] is not None:
                raise ValueError("loan already paid")

            remaining = int(row["amount"])
            if amount is None:
                pay = remaining
            else:
                if amount <= 0:
                    raise ValueError("payment must be positive")
                if amount > remaining:
                    raise ValueError("payment exceeds remaining balance")
                pay = amount

            if pay == remaining:
                cur.execute(
                    """
                    UPDATE oos_loans
                    SET amount = 0, paid_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (loan_id,),
                )
            else:
                cur.execute(
                    """
                    UPDATE oos_loans
                    SET amount = amount - %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (pay, loan_id),
                )
            updated = dict(cur.fetchone())
        con.commit()
    updated["paid_fully"] = updated["paid_at"] is not None
    updated["paid_amount"] = pay
    return updated


def delete_loan(guild_id: int, loan_id: int) -> bool:
    """Hard-delete a loan row. Returns True if a row was removed."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM oos_loans WHERE guild_id = %s AND id = %s",
                (guild_id, loan_id),
            )
            deleted = cur.rowcount > 0
        con.commit()
    return deleted
