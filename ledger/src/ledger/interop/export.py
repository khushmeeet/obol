"""Ledger -> .beancount text (plan §2.1).

A deliberately dumb serializer: it prints what is in the database, with no
inference and no computation. If the urge arises to compute something
here, that computation belongs in the ledger.

Formatting is precision-faithful: an amount stored with precision 2 prints
as ``50.00``, never ``50`` or ``50.000000``. Beancount infers tolerances
from the decimals it reads, so a formatting difference would silently
change what the oracle comparison means.

Emitted as of M7: ``option`` lines, ``commodity``, ``open``/``close``,
``pad``, ``balance``, ``note``, ``event``, ``price`` directives, and
transactions with flag, payee, narration, ``#tag``/``^link`` sets,
metadata, amounts, cost (``{...}``) and price (``@ ...``). A booked
reduction is printed one posting per matched lot, each carrying the lot's
full ``{cost, date}`` identity — the shape Beancount's own booking
produces, which lets it re-book the export identically and keeps the
oracle comparison checking our booking against theirs.

Documents are deliberately *not* exported: Beancount verifies at load
time that every document's file exists on disk, a promise a database of
stored references cannot make (and the oracle harness requires a clean
load). A document row is a pointer to a local file, not accounting data.

Metadata is exported best-effort: only keys Beancount can parse
(``[a-z][a-zA-Z0-9_-]+``, and not its internal ``filename``/``lineno``)
with scalar values — strings, booleans, and numbers. Anything else is
Obol-internal and omitted; metadata is free-form for the product
(design §4), not part of the accounting the oracle checks.

Pads are the one place the dumb-serializer rule bends, deliberately, to
keep the export a genuine oracle test: a pad that generated a padding
transaction is exported as its ``pad`` directive with the generated
transaction *omitted*, so Beancount regenerates the padding itself and
the balance comparison checks our pad arithmetic against theirs. Pads
that generated nothing (still pending, or spent without effect) are not
exported — they have no effect on balances, and Beancount rejects a pad
without a following balance check as an error ("unused pad").
"""

import datetime
import re
from collections.abc import Mapping
from decimal import Decimal

from ledger.domain.accounts import Account
from ledger.domain.amount import Amount
from ledger.domain.booking import BookingMethod
from ledger.domain.inventory import Lot
from ledger.domain.transaction import Posting, Transaction
from ledger.storage.repositories import Repository

# Ledger options with a direct Beancount equivalent. The others
# (gains_account_root, opening_balances_account) are Obol-internal and
# would be parse errors on the Beancount side. Beancount 3 renamed its
# multiplier option to "tolerance_multiplier" (the old spelling is a
# DeprecatedError on load), and its scale differs: Obol's 1.0 is
# Beancount's 0.5 (design §6 folds the "half the smallest unit" into the
# rule), so the value is halved on the way out.
_BEANCOUNT_OPTION_NAMES = {
    "operating_currency": "operating_currency",
    "inferred_tolerance_multiplier": "tolerance_multiplier",
    "default_booking_method": "booking_method",
}

# Beancount requires a date on commodity directives; the schema does not
# record one yet (first_date is a later milestone). A fixed epoch keeps
# the output deterministic and carries no meaning to Beancount.
_COMMODITY_EPOCH = "1970-01-01"

# What Beancount's parser accepts as a metadata key (verified against
# 3.2.3: lowercase first letter, at least two characters). filename and
# lineno are its own internal bookkeeping.
_METADATA_KEY_RE = re.compile(r"[a-z][a-zA-Z0-9\-_]+$")
_METADATA_RESERVED_KEYS = frozenset({"filename", "lineno"})


def _quoted(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_amount(amount: Amount) -> str:
    return f"{amount.to_decimal()} {amount.commodity.symbol}"


def _booking_string(method: BookingMethod) -> str | None:
    """The method as Beancount spells it, or None to omit.

    STRICT is Beancount's default and is left implicit. SPECIFIC has no
    Beancount equivalent; a SPECIFIC reduction always names its lot, which
    STRICT resolves unambiguously, so it degrades to the (omitted) default.
    """
    if method in (BookingMethod.STRICT, BookingMethod.SPECIFIC):
        return None
    return method.value


def _option_lines(options: dict[str, str]) -> list[str]:
    lines = []
    for key in sorted(options):
        name = _BEANCOUNT_OPTION_NAMES.get(key)
        if name is None:
            continue
        value = options[key]
        if key == "default_booking_method":
            value_or_none = _booking_string(BookingMethod(value))
            if value_or_none is None:
                continue
            value = value_or_none
        elif key == "inferred_tolerance_multiplier":
            value = str(Decimal(value) / 2)
        lines.append(f"option {_quoted(name)} {_quoted(value)}")
    return lines


def _open_line(account: Account) -> str:
    parts = [account.opened_on.isoformat(), "open", account.path]
    if account.allowed_commodities is not None:
        parts.append(",".join(sorted(account.allowed_commodities)))
    booking = _booking_string(account.booking_method)
    if booking is not None:
        parts.append(_quoted(booking))
    return " ".join(parts)


def _metadata_value(value: object) -> str | None:
    """The Beancount literal for a metadata value, or None to omit it.
    bool first: it is an int subclass, and Beancount spells it TRUE/FALSE."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return _quoted(value)
    if isinstance(value, int | float):
        return format(Decimal(str(value)), "f")
    return None


def _metadata_lines(metadata: Mapping[str, object], indent: str) -> list[str]:
    lines = []
    for key in sorted(metadata):
        if key in _METADATA_RESERVED_KEYS or not _METADATA_KEY_RE.match(key):
            continue
        value = _metadata_value(metadata[key])
        if value is not None:
            lines.append(f"{indent}{key}: {value}")
    return lines


def _lot_cost_braces(lot: Lot) -> str:
    """The full lot identity {cost, date[, "label"]} — what makes a
    reduction split unambiguous when Beancount re-books it."""
    parts = [
        f"{lot.cost.to_decimal()} {lot.cost.commodity.symbol}",
        lot.acquired_on.isoformat(),
    ]
    if lot.label is not None:
        parts.append(_quoted(lot.label))
    return "{" + ", ".join(parts) + "}"


def _posting_lines(
    posting: Posting,
    transaction_date: datetime.date,
    lots_by_id: dict[int, Lot],
) -> list[str]:
    flag = f"{posting.flag} " if posting.flag else ""
    price = f" @ {_format_amount(posting.price)}" if posting.price is not None else ""

    if posting.lot_matches:
        # A booked reduction (or a reversal's restoration) is printed one
        # line per matched lot, each with the lot's full identity — the
        # shape Beancount's own booking produces, and what lets it re-book
        # the export identically regardless of method.
        lines = []
        for match in posting.lot_matches:
            lot = lots_by_id[match.lot_id]
            units = Amount(
                value=-match.quantity,
                precision=posting.units.precision,
                commodity=posting.units.commodity,
            )
            lines.append(
                f"  {flag}{posting.account}  {_format_amount(units)}"
                f" {_lot_cost_braces(lot)}{price}"
            )
        return lines

    cost = ""
    if posting.cost is not None:
        # An acquisition; print the cost as written, with the lot date
        # only when it differs from the transaction date (Beancount
        # defaults it identically on re-parse).
        assert posting.cost.per_unit is not None
        parts = [_format_amount(posting.cost.per_unit)]
        if posting.cost.date is not None and posting.cost.date != transaction_date:
            parts.append(posting.cost.date.isoformat())
        if posting.cost.label is not None:
            parts.append(_quoted(posting.cost.label))
        cost = " {" + ", ".join(parts) + "}"

    return [f"  {flag}{posting.account}  {_format_amount(posting.units)}{cost}{price}"]


def _transaction_lines(
    transaction: Transaction, lots_by_id: dict[int, Lot]
) -> list[str]:
    header = [transaction.date.isoformat(), transaction.flag]
    if transaction.payee is not None:
        header.append(_quoted(transaction.payee))
    header.append(_quoted(transaction.narration or ""))
    header.extend(f"#{tag}" for tag in sorted(transaction.tags))
    header.extend(f"^{link}" for link in sorted(transaction.links))
    lines = [" ".join(header)]
    lines.extend(_metadata_lines(transaction.metadata, "  "))
    for posting in transaction.postings:
        lines.extend(_posting_lines(posting, transaction.date, lots_by_id))
        lines.extend(_metadata_lines(posting.metadata, "    "))
    return lines


def export_string(repository: Repository) -> str:
    """Serialize the whole ledger to Beancount text.

    Layout is deterministic: options, commodity declarations, opens,
    closes, then transactions ordered by (date, id) with postings in their
    original seq order. Byte-identical output across backends is a test
    invariant.
    """
    sections: list[list[str]] = []

    option_lines = _option_lines(repository.list_options())
    if option_lines:
        sections.append(option_lines)

    commodity_lines = [
        f"{_COMMODITY_EPOCH} commodity {commodity.symbol}"
        for commodity in repository.list_commodities()
    ]
    if commodity_lines:
        sections.append(commodity_lines)

    accounts = repository.list_accounts()
    open_lines = [
        _open_line(account)
        for account in sorted(accounts, key=lambda a: (a.opened_on, a.path))
    ]
    if open_lines:
        sections.append(open_lines)

    close_lines = [
        f"{account.closed_on.isoformat()} close {account.path}"
        for account in sorted(
            (a for a in accounts if a.closed_on is not None),
            key=lambda a: (a.closed_on, a.path),
        )
    ]
    if close_lines:
        sections.append(close_lines)

    pad_lines = [
        f"{pad.date.isoformat()} pad {pad.account} {pad.source_account}"
        for pad in repository.list_pads()
        if pad.generated_txn_id is not None
    ]
    if pad_lines:
        sections.append(pad_lines)

    balance_lines = [
        f"{assertion.date.isoformat()} balance {assertion.account}"
        f" {_format_amount(assertion.amount)}"
        for assertion in repository.list_assertions()
    ]
    if balance_lines:
        sections.append(balance_lines)

    note_lines = [
        f"{note.date.isoformat()} note {note.account} {_quoted(note.comment)}"
        for note in repository.list_notes()
    ]
    if note_lines:
        sections.append(note_lines)

    # Documents are deliberately not exported (module docstring): Beancount
    # verifies the referenced files exist, which stored paths cannot promise.

    event_lines = [
        f"{event.date.isoformat()} event {_quoted(event.type)} {_quoted(event.value)}"
        for event in repository.list_events()
    ]
    if event_lines:
        sections.append(event_lines)

    # Prices observed from transactions (origin 'transaction') are not
    # exported: the transactions that imply them are, and Beancount would
    # otherwise see the same observation twice.
    price_lines = [
        f"{price.date.isoformat()} price {price.commodity}"
        f" {_format_amount(price.price)}"
        for price in repository.list_prices()
        if price.origin != "transaction"
    ]
    if price_lines:
        sections.append(price_lines)

    lots_by_id = {lot.id: lot for lot in repository.list_lots() if lot.id is not None}
    for transaction in repository.list_transactions():
        if transaction.generated and transaction.source == "pad":
            continue  # Beancount regenerates these from the pad directive
        sections.append(_transaction_lines(transaction, lots_by_id))

    if not sections:
        return ""
    return "\n\n".join("\n".join(section) for section in sections) + "\n"
