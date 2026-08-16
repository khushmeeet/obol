"""Beancount text -> ledger (plan §8.1).

Test infrastructure, not a product feature: importing a `bean-example`
corpus buys a multi-year, semantically rich, independently verified
fixture for the cost of this adapter (design §14, layer 3). Parsing is
delegated to `beancount.loader` — there is no reason to write a parser —
and its directives are mapped onto public `Ledger` calls only.

This module depends on `beancount`, a dev-only dependency. It is never
imported by the runtime path; the `Ledger.import_beancount` facade
imports it lazily.

Mapping notes:

- The loader's output is *booked*: reductions carry their resolved lot
  identity ``{cost, date[, "label"]}``, which our STRICT booking matches
  against the lot it named. Booking still runs for real on our side —
  the corpus comparison is what proves it agrees with Beancount's.
- Postings the loader interpolated (marked ``__automatic__``) are
  stripped and re-collapsed into a single open posting, so our own
  interpolation fills them and the balance comparison checks the fill.
  When the shape does not allow that (automatic postings across several
  accounts, or carrying cost or price), they are imported as written.
- Padding transactions (flag 'P') that a ``pad`` directive generated are
  skipped and the directive imported instead; our own pad machinery
  regenerates the padding when the next assertion is evaluated, so the
  comparison checks our pad arithmetic against Beancount's. Only 'P'
  transactions that match a pad directive's (date, account, source)
  shape are treated this way.
- Balance directives are stored and evaluated at their entry-order
  position; a failing assertion is an import error (the source ledger
  considered it true).
- ``option`` values with no ledger equivalent (title, plugins, ...) are
  ignored. `Query` and `Custom` directives are recorded as skipped.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from beancount import loader
from beancount.core import data
from beancount.core.flags import FLAG_PADDING
from beancount.core.interpolate import AUTOMATIC_META

from ledger.domain.accounts import type_for_path
from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.booking import BookingMethod
from ledger.domain.directives import AssertionStatus
from ledger.domain.errors import LedgerError
from ledger.domain.transaction import CostSpec, PostingSpec, TransactionSpec

if TYPE_CHECKING:
    import os

    from ledger.api import Ledger

# Beancount booking methods with an Obol equivalent. NONE, AVERAGE, HIFO,
# and STRICT_WITH_SIZE are deliberately unsupported (design §7, §16).
_BOOKING_METHODS = {
    "STRICT": BookingMethod.STRICT,
    "FIFO": BookingMethod.FIFO,
    "LIFO": BookingMethod.LIFO,
}

# Beancount's default tolerance multiplier. Its scale is half of Obol's
# (our 1.0 ≡ their 0.5, design §6), so the value is doubled on the way in
# — the mirror of the halving in interop/export.py.
_BEANCOUNT_DEFAULT_MULTIPLIER = Decimal("0.5")


@dataclass
class ImportReport:
    """What an import did: directive counts, what was skipped, and every
    failure. `ok` is the corpus gate's bar — nothing skipped silently
    matters, nothing failed."""

    counts: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def _count(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1


def import_file(ledger: Ledger, path: str | os.PathLike[str]) -> ImportReport:
    """Load a .beancount file and replay it into `ledger`."""
    entries, errors, options_map = loader.load_file(str(path))
    return _import_entries(ledger, entries, errors, options_map)


def import_string(ledger: Ledger, text: str) -> ImportReport:
    """Load Beancount text and replay it into `ledger`."""
    entries, errors, options_map = loader.load_string(text)
    return _import_entries(ledger, entries, errors, options_map)


def _clean_metadata(meta: object) -> dict[str, object]:
    """Beancount metadata as the ledger can store it (JSON scalars).

    The loader's own bookkeeping (filename, lineno, dunder keys like
    __tolerances__ and __automatic__) is dropped; dates and Decimals are
    stringified losslessly; anything exotic degrades to str().
    """
    out: dict[str, object] = {}
    if not isinstance(meta, dict):
        return out
    for key, value in meta.items():
        if key in ("filename", "lineno") or key.startswith("__"):
            continue
        if value is None:
            continue
        if isinstance(value, bool | int | float | str):
            out[key] = value
        elif isinstance(value, Decimal):
            # The parser reads every number as Decimal; keep integers as
            # written integers, stringify the rest losslessly ("3.00"
            # keeps its written precision, which int() would drop).
            exponent = value.as_tuple().exponent
            if isinstance(exponent, int) and exponent >= 0:
                out[key] = int(value)
            else:
                out[key] = str(value)
        elif isinstance(value, datetime.date):
            out[key] = value.isoformat()
        else:
            out[key] = str(value)
    return out


def _classify_commodities(
    entries: list[data.Directive], operating: set[str]
) -> dict[str, CommodityKind]:
    """Every commodity symbol the entries use, with an inferred kind.

    Kind is display metadata — it drives no accounting semantics — so a
    heuristic is enough: something held at cost or quoted by a price is a
    security; something used to quote costs and prices (or declared as an
    operating currency) is a currency; anything left is a tracking unit
    (vacation hours, 401k allowances — units that flow through postings
    but are never priced).
    """
    seen: set[str] = set()
    quotes: set[str] = set()
    priced: set[str] = set()
    for entry in entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                if posting.units is None or posting.units.currency is None:
                    continue
                seen.add(posting.units.currency)
                if posting.cost is not None and posting.cost.currency is not None:
                    seen.add(posting.cost.currency)
                    quotes.add(posting.cost.currency)
                    priced.add(posting.units.currency)
                if posting.price is not None and posting.price.currency is not None:
                    seen.add(posting.price.currency)
                    quotes.add(posting.price.currency)
                    priced.add(posting.units.currency)
        elif isinstance(entry, data.Balance):
            seen.add(entry.amount.currency)
        elif isinstance(entry, data.Price):
            seen.add(entry.currency)
            priced.add(entry.currency)
            if entry.amount is not None and entry.amount.currency is not None:
                seen.add(entry.amount.currency)
                quotes.add(entry.amount.currency)
        elif isinstance(entry, data.Commodity):
            seen.add(entry.currency)

    kinds: dict[str, CommodityKind] = {}
    for symbol in seen:
        if symbol in operating or (symbol in quotes and symbol not in priced):
            kinds[symbol] = CommodityKind.CURRENCY
        elif symbol in priced:
            kinds[symbol] = CommodityKind.SECURITY
        else:
            kinds[symbol] = CommodityKind.TRACKING
    return kinds


def _map_options(
    ledger: Ledger, options_map: dict[str, object], report: ImportReport
) -> None:
    operating = options_map.get("operating_currency") or []
    if isinstance(operating, list) and operating:
        ledger.set_option("operating_currency", str(operating[0]))
        report._count("option")
        if len(operating) > 1:
            report.skipped.append(
                f"extra operating currencies {operating[1:]} (the ledger stores one)"
            )

    # Beancount 3 renamed the option to "tolerance_multiplier"; the old
    # options_map key survives but no longer tracks the file's value.
    multiplier = options_map.get(
        "tolerance_multiplier", options_map.get("inferred_tolerance_multiplier")
    )
    if isinstance(multiplier, Decimal) and multiplier != _BEANCOUNT_DEFAULT_MULTIPLIER:
        ledger.set_option("inferred_tolerance_multiplier", str(multiplier * 2))
        report._count("option")

    booking = options_map.get("booking_method")
    if booking is not None and getattr(booking, "name", None) != "STRICT":
        name = getattr(booking, "name", str(booking))
        if name in _BOOKING_METHODS:
            ledger.set_option("default_booking_method", name)
            report._count("option")
        else:
            report.errors.append(f"unsupported ledger-wide booking method {name}")


def _booking_for(
    entry: data.Open,
    default: BookingMethod,
    report: ImportReport,
) -> BookingMethod:
    if entry.booking is None:
        return default
    method = _BOOKING_METHODS.get(entry.booking.name)
    if method is None:
        report.errors.append(
            f"{entry.date} open {entry.account}: unsupported booking"
            f" method {entry.booking.name} (using STRICT)"
        )
        return BookingMethod.STRICT
    return method


def _collapse_automatic(
    postings: list[data.Posting],
) -> tuple[list[data.Posting], str | None]:
    """Separate loader-interpolated postings from written ones.

    Returns (written postings, account to leave open). The interpolated
    postings collapse back into one open posting — the shape the source
    file had — when they all sit on one account and carry no cost or
    price; otherwise everything is imported as written (open account None
    means nothing was stripped).
    """
    automatic = [
        posting
        for posting in postings
        if posting.meta and posting.meta.get(AUTOMATIC_META)
    ]
    if not automatic:
        return postings, None
    accounts = {posting.account for posting in automatic}
    if len(accounts) != 1 or any(
        posting.cost is not None or posting.price is not None for posting in automatic
    ):
        return postings, None
    automatic_ids = {id(posting) for posting in automatic}
    return [p for p in postings if id(p) not in automatic_ids], accounts.pop()


def _posting_spec(
    posting: data.Posting, commodities: dict[str, Commodity]
) -> PostingSpec:
    units = Amount.from_decimal(
        posting.units.number, commodities[posting.units.currency]
    )
    cost = None
    if posting.cost is not None:
        cost = CostSpec(
            per_unit=posting.cost.number,
            commodity=posting.cost.currency,
            date=posting.cost.date,
            label=posting.cost.label,
        )
    price = None
    if posting.price is not None and posting.price.number is not None:
        price = Amount.from_decimal(
            posting.price.number, commodities[posting.price.currency]
        )
    return PostingSpec(
        account=posting.account,
        units=units,
        cost=cost,
        price=price,
        flag=posting.flag,
        metadata=_clean_metadata(posting.meta),
    )


def _import_entries(
    ledger: Ledger,
    entries: list[data.Directive],
    load_errors: list[object],
    options_map: dict[str, object],
) -> ImportReport:
    report = ImportReport()
    if load_errors:
        report.errors.extend(
            f"beancount load error: {getattr(error, 'message', error)}"
            for error in load_errors
        )
        return report

    _map_options(ledger, options_map, report)

    operating = {str(symbol) for symbol in options_map.get("operating_currency") or []}
    kinds = _classify_commodities(entries, operating)
    declarations = {
        entry.currency: entry for entry in entries if isinstance(entry, data.Commodity)
    }
    commodities: dict[str, Commodity] = {}
    for symbol in sorted(kinds):
        registered = ledger.get_commodity(symbol)
        if registered is not None:
            # Importing into a ledger that already knows this commodity:
            # reuse its definition, whatever kind it was registered with.
            commodities[symbol] = registered
            continue
        declared = declarations.get(symbol)
        meta = _clean_metadata(declared.meta if declared is not None else None)
        name = meta.pop("name", None)
        try:
            commodities[symbol] = ledger.create_commodity(
                symbol,
                kinds[symbol],
                name=str(name) if name is not None else None,
                metadata=meta or None,
            )
            report._count("commodity")
        except LedgerError as exc:
            report.errors.append(f"commodity {symbol}: {exc}")

    default_booking = _BOOKING_METHODS.get(
        getattr(options_map.get("booking_method"), "name", "STRICT"),
        BookingMethod.STRICT,
    )
    # The (date, account, source_account) shapes of pad directives, used
    # to recognize the padding transactions the loader generated for them.
    pad_shapes = {
        (entry.date, entry.account, entry.source_account)
        for entry in entries
        if isinstance(entry, data.Pad)
    }

    for entry in entries:
        try:
            if isinstance(entry, data.Open):
                ledger.create_account(
                    entry.account,
                    type_for_path(entry.account),
                    entry.date,
                    booking_method=_booking_for(entry, default_booking, report),
                    allowed_commodities=entry.currencies or None,
                    metadata=_clean_metadata(entry.meta) or None,
                )
                report._count("open")
            elif isinstance(entry, data.Close):
                ledger.close_account(entry.account, entry.date)
                report._count("close")
            elif isinstance(entry, data.Commodity):
                pass  # created up front
            elif isinstance(entry, data.Transaction):
                _import_transaction(ledger, entry, commodities, pad_shapes, report)
            elif isinstance(entry, data.Balance):
                _import_balance(ledger, entry, report)
            elif isinstance(entry, data.Pad):
                ledger.pad(entry.account, entry.source_account, entry.date)
                report._count("pad")
            elif isinstance(entry, data.Price):
                if entry.amount is None or entry.amount.number is None:
                    report.skipped.append(
                        f"{entry.date} price {entry.currency}: no amount"
                    )
                else:
                    ledger.record_price(
                        entry.currency,
                        entry.date,
                        entry.amount.number,
                        entry.amount.currency,
                        origin="directive",
                    )
                    report._count("price")
            elif isinstance(entry, data.Note):
                ledger.add_note(entry.account, entry.date, entry.comment)
                report._count("note")
            elif isinstance(entry, data.Document):
                ledger.add_document(entry.account, entry.date, entry.filename)
                report._count("document")
            elif isinstance(entry, data.Event):
                ledger.add_event(entry.date, entry.type, entry.description)
                report._count("event")
            else:
                report.skipped.append(
                    f"{entry.date} {type(entry).__name__}: not imported"
                )
        except LedgerError as exc:
            report.errors.append(f"{entry.date} {type(entry).__name__}: {exc}")
    return report


def _import_transaction(
    ledger: Ledger,
    entry: data.Transaction,
    commodities: dict[str, Commodity],
    pad_shapes: set[tuple[datetime.date, str, str]],
    report: ImportReport,
) -> None:
    if entry.flag == FLAG_PADDING and len(entry.postings) == 2:
        accounts = [posting.account for posting in entry.postings]
        if (entry.date, accounts[0], accounts[1]) in pad_shapes or (
            entry.date,
            accounts[1],
            accounts[0],
        ) in pad_shapes:
            # Generated padding; the pad directive is imported instead and
            # our own machinery regenerates this transaction.
            report._count("padding-skipped")
            return

    written, open_account = _collapse_automatic(list(entry.postings))
    for posting in written:
        if posting.units is None or posting.units.number is None:
            report.errors.append(
                f"{entry.date} Transaction: posting on {posting.account}"
                f" has no resolved amount"
            )
            return
    specs = [_posting_spec(posting, commodities) for posting in written]
    if open_account is not None:
        specs.append(PostingSpec(account=open_account))

    ledger.record(
        TransactionSpec(
            date=entry.date,
            postings=specs,
            flag=entry.flag,
            payee=entry.payee or None,
            narration=entry.narration or None,
            tags=set(entry.tags or ()),
            links=set(entry.links or ()),
            metadata=_clean_metadata(entry.meta),
        )
    )
    report._count("transaction")


def _import_balance(ledger: Ledger, entry: data.Balance, report: ImportReport) -> None:
    if entry.tolerance is not None:
        report.errors.append(
            f"{entry.date} balance {entry.account}: explicit tolerance"
            f" (~ {entry.tolerance}) is not supported"
        )
        return
    assertion = ledger.assert_balance(
        entry.account,
        entry.date,
        entry.amount.number,
        entry.amount.currency,
        source="import",
    )
    report._count("balance")
    if assertion.status is not AssertionStatus.PASS:
        difference = (
            assertion.difference.to_decimal()
            if assertion.difference is not None
            else "?"
        )
        report.errors.append(
            f"{entry.date} balance {entry.account}: asserted"
            f" {entry.amount.number} {entry.amount.currency},"
            f" difference {difference}"
        )
