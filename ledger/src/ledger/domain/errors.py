"""Typed exceptions for the ledger.

Every error raised by the library derives from LedgerError, so callers can
catch the whole family with one clause or handle specific failures.
"""

from decimal import Decimal


class LedgerError(Exception):
    """Base class for every error raised by the ledger."""


# --- amounts ---------------------------------------------------------------


class AmountError(LedgerError):
    pass


class CommodityMismatchError(AmountError):
    """Arithmetic or comparison attempted across different commodities."""


class PrecisionError(AmountError):
    """An amount cannot be represented at the global scale of 8 decimals."""


class AmountRangeError(AmountError):
    """An amount overflows the int64 range at scale 8."""


# --- commodities -----------------------------------------------------------


class CommodityError(LedgerError):
    pass


class InvalidCommodityError(CommodityError):
    """Symbol or precision fails validation."""


class UnknownCommodityError(CommodityError):
    """A posting references a commodity that was never registered."""


class DuplicateCommodityError(CommodityError):
    """A commodity with this symbol already exists."""


class CommodityNotAllowedError(CommodityError):
    """A posting's commodity violates its account's allowed-commodity list."""


# --- accounts --------------------------------------------------------------


class AccountError(LedgerError):
    pass


class InvalidAccountPathError(AccountError):
    """Path fails structural validation (segments, characters, root)."""


class AccountTypeMismatchError(AccountError):
    """Account type disagrees with the path's root segment."""


class UnknownAccountError(AccountError):
    """A posting references an account that was never opened."""


class AccountNotOpenError(AccountError):
    """The transaction date falls outside the account's open/close window."""


class DuplicateAccountError(AccountError):
    """An account with this path already exists."""


# --- transactions ----------------------------------------------------------


class TransactionError(LedgerError):
    pass


class InvalidTransactionError(TransactionError):
    """Structurally invalid transaction (e.g. fewer than two postings)."""


class UnbalancedTransactionError(TransactionError):
    """Per-commodity weights do not sum to zero within tolerance.

    Carries the offending residuals and the inferred tolerances, keyed by
    commodity symbol, so callers can render the failure precisely.
    """

    def __init__(
        self,
        residuals: dict[str, Decimal],
        tolerances: dict[str, Decimal],
    ) -> None:
        self.residuals = residuals
        self.tolerances = tolerances
        parts = ", ".join(
            f"{symbol}: residual {residual} exceeds tolerance "
            f"{tolerances.get(symbol, Decimal(0))}"
            for symbol, residual in sorted(residuals.items())
        )
        super().__init__(f"transaction does not balance: {parts}")


class InterpolationError(TransactionError):
    """Missing amounts cannot be inferred (too many open postings, or none
    needed)."""


class DuplicateSourceError(TransactionError):
    """A transaction with the same (source, source_ref) already exists."""


class UnknownTransactionError(TransactionError):
    """A correction references a transaction id that does not exist."""


class ReversalError(TransactionError):
    """A transaction cannot be reversed or replaced: it is machine-generated,
    is itself a reversal, or the correction would be backdated."""


class AlreadyReversedError(ReversalError):
    """The transaction already has a reversal."""


class InvalidTagError(TransactionError):
    """A tag name is empty or uses characters Beancount cannot represent."""


class InvalidLinkError(TransactionError):
    """A link name is empty or uses characters Beancount cannot represent."""


# --- cost, lots, and booking (design §7) ------------------------------------


class InvalidCostError(TransactionError):
    """A cost is structurally invalid: negative, missing its commodity on
    an acquisition, attached to zero units, or its commodity disagrees
    with the posting's price commodity (Beancount: "Cost and price
    currencies must match", verified against 3.2.3)."""


class InvalidPriceError(TransactionError):
    """A price is structurally invalid (negative — Beancount refuses
    negative prices; zero is allowed, verified against 3.2.3)."""


class BookingError(TransactionError):
    """A reduction cannot be resolved against the account's lots."""


class NoLotMatchError(BookingError):
    """No lot matches the reduction's cost filter (Beancount: "No position
    matches")."""


class AmbiguousLotError(BookingError):
    """Under STRICT (or SPECIFIC) booking, the filter leaves more than one
    lot and the reduction does not consume their total exactly (Beancount:
    "Ambiguous matches")."""


class InsufficientLotError(BookingError):
    """The matched lots hold less than the reduction needs (Beancount:
    "Not enough lots to reduce")."""


# --- prices and valuation (design §9) ---------------------------------------


class PriceError(LedgerError):
    pass


class MissingPriceError(PriceError):
    """Market valuation needs a price the price table does not have."""


# --- directives ------------------------------------------------------------


class DirectiveError(LedgerError):
    pass


class PadError(DirectiveError):
    """A pad directive is structurally invalid (padding an account from
    itself, or against accounts that do not exist or are not open)."""


class NoteError(DirectiveError):
    """A note is structurally invalid (empty comment)."""


class DocumentError(DirectiveError):
    """A document reference is structurally invalid (empty path, or a
    malformed SHA-256)."""


class EventError(DirectiveError):
    """An event is structurally invalid (empty type)."""


# --- general ---------------------------------------------------------------


class NotSupportedError(LedgerError):
    """Feature is designed but not yet implemented (e.g. cost/price before
    M7)."""


class OptionError(LedgerError):
    """Unknown option key or invalid option value."""


class StorageError(LedgerError):
    pass
