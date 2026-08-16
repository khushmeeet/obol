"""The Beancount importer (plan §8.1), on both backends.

The corpus gate proves the importer on bean-example output; these tests
pin the mappings bean-example never exercises — pads, loader-interpolated
postings, option translation, unsupported booking methods — and the
failure modes, on small hand-written files.
"""

import datetime
from decimal import Decimal

import pytest
from beancount.core.data import Posting as BcPosting

from ledger.domain.amount import CommodityKind
from ledger.domain.booking import BookingMethod
from ledger.domain.directives import AssertionStatus
from ledger.interop.import_ import _collapse_automatic

D = datetime.date


def test_basic_file_round_trips(ledger):
    report = ledger.import_beancount_string(
        """
option "operating_currency" "USD"

2024-01-01 commodity USD
  name: "US Dollar"

2024-01-01 open Assets:Cash USD
2024-01-01 open Expenses:Food

2024-01-10 * "Cafe" "Lunch" #food ^receipt-1
  note: "with friends"
  Assets:Cash  -20.00 USD
  Expenses:Food  20.00 USD

2024-01-15 balance Assets:Cash -20.00 USD

2024-01-20 price USD 0.92 EUR

2024-02-01 event "location" "Lisbon"

2024-02-02 note Assets:Cash "Switched banks"
"""
    )
    assert report.ok, report.errors
    assert not report.skipped
    assert report.counts == {
        "option": 1,
        "commodity": 2,  # USD and EUR (the price quote)
        "open": 2,
        "transaction": 1,
        "balance": 1,
        "price": 1,
        "event": 1,
        "note": 1,
    }

    assert ledger.get_option("operating_currency") == "USD"
    usd = ledger.get_commodity("USD")
    assert usd is not None and usd.kind is CommodityKind.CURRENCY
    cash = ledger.get_account("Assets:Cash")
    assert cash is not None
    assert cash.allowed_commodities == frozenset({"USD"})
    assert cash.opened_on == D(2024, 1, 1)

    [txn] = ledger.list_transactions()
    assert (txn.payee, txn.narration) == ("Cafe", "Lunch")
    assert txn.tags == frozenset({"food"})
    assert txn.links == frozenset({"receipt-1"})
    assert txn.metadata == {"note": "with friends"}
    assert ledger.balance("Assets:Cash").to_dict() == {"USD": Decimal("-20.00")}

    [assertion] = ledger.list_assertions()
    assert assertion.status is AssertionStatus.PASS
    assert ledger.get_price("USD", "EUR") is not None
    assert [event.value for event in ledger.list_events()] == ["Lisbon"]
    assert [note.comment for note in ledger.list_notes()] == ["Switched banks"]


def test_loader_interpolated_posting_becomes_ours(ledger):
    """An amountless posting is stripped of Beancount's fill and left
    open, so our interpolation fills it — and marks it interpolated."""
    report = ledger.import_beancount_string(
        """
2024-01-01 open Assets:Cash
2024-01-01 open Expenses:Food

2024-01-02 * "lunch"
  Assets:Cash  -50.00 USD
  Expenses:Food
"""
    )
    assert report.ok, report.errors
    [txn] = ledger.list_transactions()
    [filled] = [posting for posting in txn.postings if posting.interpolated]
    assert filled.account == "Expenses:Food"
    assert filled.units.to_decimal() == Decimal("50.00")


def test_multi_currency_interpolation_collapses_to_one_open_posting(ledger):
    """Beancount expands one amountless posting into one fill per
    residual currency; the importer collapses them back and our
    interpolation re-expands identically."""
    report = ledger.import_beancount_string(
        """
2024-01-01 open Assets:Cash
2024-01-01 open Income:Salary

2024-01-02 * "pay"
  Income:Salary  -50.00 USD
  Income:Salary  -5 VAC
  Assets:Cash
"""
    )
    assert report.ok, report.errors
    [txn] = ledger.list_transactions()
    filled = [posting for posting in txn.postings if posting.interpolated]
    assert {
        (posting.account, posting.units.to_decimal(), posting.units.commodity.symbol)
        for posting in filled
    } == {("Assets:Cash", Decimal("50.00"), "USD"), ("Assets:Cash", Decimal(5), "VAC")}
    assert ledger.balance("Assets:Cash").to_dict() == {
        "USD": Decimal("50.00"),
        "VAC": Decimal(5),
    }


def test_pad_is_imported_and_regenerates_the_padding(ledger):
    """The loader's generated padding transaction (flag P) is skipped;
    the pad directive is imported and our own machinery regenerates the
    padding when the assertion is evaluated."""
    report = ledger.import_beancount_string(
        """
2024-01-01 open Assets:Bank
2024-01-01 open Equity:Opening-Balances

2024-01-05 pad Assets:Bank Equity:Opening-Balances

2024-01-10 balance Assets:Bank 500.00 USD
"""
    )
    assert report.ok, report.errors
    assert report.counts.get("pad") == 1
    assert report.counts.get("padding-skipped") == 1
    assert "transaction" not in report.counts

    [assertion] = ledger.list_assertions()
    assert assertion.status is AssertionStatus.PASS
    [pad] = ledger.list_pads()
    assert pad.generated_txn_id is not None
    generated = ledger.get_transaction(pad.generated_txn_id)
    assert generated is not None and generated.date == D(2024, 1, 5)
    assert ledger.balance("Assets:Bank").to_dict() == {"USD": Decimal("500.00")}


def test_user_p_flag_transaction_without_a_pad_is_imported(ledger):
    """Only P transactions matching a pad directive's shape are treated
    as generated padding."""
    report = ledger.import_beancount_string(
        """
2024-01-01 open Assets:Cash
2024-01-01 open Expenses:Food

2024-01-02 P "user-flagged"
  Assets:Cash  -10.00 USD
  Expenses:Food  10.00 USD
"""
    )
    assert report.ok, report.errors
    assert report.counts.get("transaction") == 1
    assert "padding-skipped" not in report.counts
    [txn] = ledger.list_transactions()
    assert txn.flag == "P"


def test_fifo_trading_imports_with_lots_and_gain(ledger):
    report = ledger.import_beancount_string(
        """
2024-01-01 open Assets:Broker "FIFO"
2024-01-01 open Assets:Cash
2024-01-01 open Income:Gains

2024-01-02 * "buy one"
  Assets:Broker  10 STK {10.00 USD}
  Assets:Cash  -100.00 USD

2024-01-03 * "buy two"
  Assets:Broker  10 STK {12.00 USD}
  Assets:Cash  -120.00 USD

2024-01-04 * "sell across lots"
  Assets:Broker  -15 STK {} @ 20.00 USD
  Assets:Cash  300.00 USD
  Income:Gains
"""
    )
    assert report.ok, report.errors
    broker = ledger.get_account("Assets:Broker")
    assert broker is not None and broker.booking_method is BookingMethod.FIFO
    # FIFO: all of lot one (basis 100.00) plus 5 of lot two (60.00);
    # proceeds 300.00 -> gain 140.00.
    assert ledger.balance("Income:Gains").to_dict() == {"USD": Decimal("-140.00")}
    [position] = [
        position
        for position in ledger.inventory("Assets:Broker").positions()
        if position.cost is not None
    ]
    assert position.units.to_decimal() == Decimal(5)
    assert position.cost.per_unit.to_decimal() == Decimal("12.00")


def test_file_wide_booking_option_applies_to_accounts(ledger):
    report = ledger.import_beancount_string(
        """
option "booking_method" "FIFO"

2024-01-01 open Assets:Broker
2024-01-01 open Assets:Named "LIFO"
"""
    )
    assert report.ok, report.errors
    assert ledger.get_option("default_booking_method") == "FIFO"
    broker = ledger.get_account("Assets:Broker")
    named = ledger.get_account("Assets:Named")
    assert broker is not None and broker.booking_method is BookingMethod.FIFO
    assert named is not None and named.booking_method is BookingMethod.LIFO


def test_tolerance_multiplier_is_rescaled(ledger):
    """Beancount's multiplier is half of Obol's (design §6): their 0.2
    imports as our 0.4, and their 0.5 default imports as nothing."""
    report = ledger.import_beancount_string(
        'option "tolerance_multiplier" "0.2"\n\n2024-01-01 open Assets:Cash\n'
    )
    assert report.ok, report.errors
    assert ledger.get_option("inferred_tolerance_multiplier") == "0.4"


def test_default_tolerance_multiplier_is_not_stored(ledger):
    report = ledger.import_beancount_string("2024-01-01 open Assets:Cash\n")
    assert report.ok, report.errors
    assert ledger.get_option("inferred_tolerance_multiplier") is None


def test_unsupported_booking_method_is_an_error(ledger):
    report = ledger.import_beancount_string('2024-01-01 open Assets:Broker "NONE"\n')
    assert not report.ok
    assert "NONE" in report.errors[0]
    # The account still exists (as STRICT) so the rest can import.
    assert ledger.get_account("Assets:Broker") is not None


def test_explicit_assertion_tolerance_is_an_error(ledger):
    report = ledger.import_beancount_string(
        """
2024-01-01 open Assets:Cash

2024-01-02 * "seed"
  Assets:Cash  100.00 USD
  Assets:Cash  -100.00 USD

2024-01-03 balance Assets:Cash 0.00 ~ 0.05 USD
"""
    )
    assert not report.ok
    assert "tolerance" in report.errors[0]


def test_loader_errors_abort_the_import(ledger):
    report = ledger.import_beancount_string(
        """
2024-01-01 open Assets:Cash
2024-01-01 open Expenses:Food

2024-01-02 * "does not balance"
  Assets:Cash  -50.00 USD
  Expenses:Food  49.00 USD
"""
    )
    assert not report.ok
    assert "load error" in report.errors[0]
    assert not report.counts
    assert ledger.list_transactions() == []


def test_registered_commodities_are_reused(ledger):
    """Importing into a ledger that already knows a commodity keeps the
    existing definition instead of failing on the duplicate."""
    ledger.create_commodity("USD", CommodityKind.CURRENCY, 2)
    report = ledger.import_beancount_string(
        """
2024-01-01 open Assets:Cash

2024-01-02 * "seed"
  Assets:Cash  1.00 USD
  Assets:Cash  -1.00 USD
"""
    )
    assert report.ok, report.errors
    assert "commodity" not in report.counts


def test_metadata_values_are_stored_as_json_scalars(ledger):
    report = ledger.import_beancount_string(
        """
2024-01-01 open Assets:Cash
2024-01-01 open Expenses:Food

2024-01-02 * "typed metadata"
  count: 3
  rate: 1.25
  when: 2024-01-02
  ok: TRUE
  Assets:Cash  -10.00 USD
    kind: "cash"
  Expenses:Food  10.00 USD
"""
    )
    assert report.ok, report.errors
    [txn] = ledger.list_transactions()
    assert txn.metadata == {
        "count": 3,
        "rate": "1.25",  # Decimal, stringified losslessly
        "when": "2024-01-02",
        "ok": True,
    }
    assert txn.postings[0].metadata == {"kind": "cash"}


def test_document_imports_when_its_file_exists(ledger, tmp_path):
    """Beancount refuses to load a document whose file is missing, so the
    happy path needs a real file."""
    statement = tmp_path / "statement.pdf"
    statement.write_bytes(b"pdf")
    report = ledger.import_beancount_string(
        f"""
2024-01-01 open Assets:Cash

2024-01-05 document Assets:Cash "{statement}"
"""
    )
    assert report.ok, report.errors
    [document] = ledger.list_documents()
    assert document.path == str(statement)


def _bc_posting(account, meta=None, cost=None):
    return BcPosting(
        account=account, units=None, cost=cost, price=None, flag=None, meta=meta
    )


def test_collapse_declines_automatic_postings_on_several_accounts():
    postings = [
        _bc_posting("Assets:A", meta={"__automatic__": True}),
        _bc_posting("Assets:B", meta={"__automatic__": True}),
        _bc_posting("Income:C"),
    ]
    written, open_account = _collapse_automatic(postings)
    assert written == postings
    assert open_account is None


def test_collapse_declines_automatic_postings_with_cost():
    postings = [
        _bc_posting("Assets:A", meta={"__automatic__": True}, cost=object()),
        _bc_posting("Income:C"),
    ]
    written, open_account = _collapse_automatic(postings)
    assert written == postings
    assert open_account is None


def test_collapse_merges_same_account_automatic_postings():
    auto_one = _bc_posting("Assets:A", meta={"__automatic__": True})
    auto_two = _bc_posting("Assets:A", meta={"__automatic__": True})
    written_posting = _bc_posting("Income:C")
    written, open_account = _collapse_automatic([auto_one, written_posting, auto_two])
    assert written == [written_posting]
    assert open_account == "Assets:A"


@pytest.mark.parametrize("origin", ["file", "string"])
def test_facade_accepts_files_and_strings(ledger, tmp_path, origin):
    text = "2024-01-01 open Assets:Cash\n"
    if origin == "file":
        path = tmp_path / "mini.beancount"
        path.write_text(text)
        report = ledger.import_beancount(path)
    else:
        report = ledger.import_beancount_string(text)
    assert report.ok, report.errors
    assert report.counts.get("open") == 1
