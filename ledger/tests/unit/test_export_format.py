"""Exporter formatting rules (plan §2.1), tested one at a time.

The golden byte-compares cover whole-ledger output; these pin the
individual rules — quoting, precision-faithful amounts, booking strings,
option mapping — so a violation names itself instead of surfacing as a
byte diff. The exporter is a dumb serializer: every test here asserts
formatting, never computation.
"""

import datetime
from decimal import Decimal

import pytest

from ledger.api import Ledger
from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.transaction import PostingSpec, TransactionSpec
from ledger.storage.repositories import InMemoryRepository

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
JAN1 = datetime.date(2024, 1, 1)


@pytest.fixture
def led() -> Ledger:
    return Ledger(repository=InMemoryRepository())


def usd(text: str) -> Amount:
    return Amount.from_decimal(Decimal(text), USD)


def simple_setup(led: Ledger) -> None:
    led.create_commodity("USD", "currency")
    led.create_account("Assets:Cash", "asset", JAN1)
    led.create_account("Expenses:Misc", "expense", JAN1)


def record_pair(led: Ledger, amount: str, **kwargs) -> None:
    led.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 5),
            postings=[
                PostingSpec(account="Assets:Cash", units=usd(amount)),
                PostingSpec(account="Expenses:Misc"),
            ],
            **kwargs,
        )
    )


def test_empty_ledger_exports_empty_string(led: Ledger):
    assert led.export_beancount_string() == ""


def test_only_options_beancount_understands_are_emitted(led: Ledger):
    led.set_option("operating_currency", "USD")
    led.set_option("inferred_tolerance_multiplier", "1.1")
    led.set_option("gains_account_root", "Income:Gains")
    led.set_option("opening_balances_account", "Equity:Opening-Balances")
    text = led.export_beancount_string()
    assert 'option "operating_currency" "USD"' in text
    # Beancount 3 spells it tolerance_multiplier, at half Obol's scale
    # (our 1.0 is its 0.5, design §6).
    assert 'option "tolerance_multiplier" "0.55"' in text
    assert "inferred_tolerance_multiplier" not in text
    assert "gains_account_root" not in text
    assert "opening_balances_account" not in text


def test_default_booking_method_maps_to_beancount_booking_method(led: Ledger):
    led.set_option("default_booking_method", "FIFO")
    assert 'option "booking_method" "FIFO"' in led.export_beancount_string()


def test_strict_default_booking_is_left_implicit(led: Ledger):
    led.set_option("default_booking_method", "STRICT")
    assert "booking_method" not in led.export_beancount_string()


def test_open_line_carries_sorted_currencies_and_booking_string(led: Ledger):
    led.create_commodity("USD", "currency")
    led.create_commodity("EUR", "currency")
    led.create_account(
        "Assets:Broker",
        "asset",
        JAN1,
        booking_method="FIFO",
        allowed_commodities=["USD", "EUR"],
    )
    led.create_account("Assets:Cash", "asset", datetime.date(2024, 1, 2))
    text = led.export_beancount_string()
    assert '2024-01-01 open Assets:Broker EUR,USD "FIFO"' in text
    assert "2024-01-02 open Assets:Cash\n" in text


def test_specific_booking_degrades_to_the_implicit_default(led: Ledger):
    led.create_account("Assets:Broker", "asset", JAN1, booking_method="SPECIFIC")
    assert "2024-01-01 open Assets:Broker\n" in led.export_beancount_string()


def test_close_line(led: Ledger):
    led.create_account("Assets:Old", "asset", JAN1)
    led.close_account("Assets:Old", datetime.date(2024, 6, 30))
    assert "2024-06-30 close Assets:Old" in led.export_beancount_string()


def test_payee_without_narration_gets_an_empty_narration_string(led: Ledger):
    simple_setup(led)
    record_pair(led, "-5.00", payee="Landlord")
    assert '2024-01-05 * "Landlord" ""' in led.export_beancount_string()


def test_quotes_and_backslashes_are_escaped(led: Ledger):
    simple_setup(led)
    record_pair(led, "-5.00", payee='Bob "The Builder"', narration="back\\slash")
    header = '2024-01-05 * "Bob \\"The Builder\\"" "back\\\\slash"'
    assert header in led.export_beancount_string()


def test_transaction_and_posting_flags_are_printed(led: Ledger):
    simple_setup(led)
    led.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 5),
            flag="!",
            postings=[
                PostingSpec(account="Assets:Cash", units=usd("-5.00"), flag="!"),
                PostingSpec(account="Expenses:Misc"),
            ],
        )
    )
    text = led.export_beancount_string()
    assert '2024-01-05 ! ""' in text
    assert "  ! Assets:Cash  -5.00 USD" in text


def test_amounts_print_exactly_as_written(led: Ledger):
    """Precision-faithful formatting: Beancount infers tolerances from the
    decimals it reads, so 50.0 must not become 50 or 50.00."""
    simple_setup(led)
    record_pair(led, "-50.0")
    text = led.export_beancount_string()
    assert "  Assets:Cash  -50.0 USD" in text
    # The interpolated leg carries the written precision of its commodity.
    assert "  Expenses:Misc  50.0 USD" in text


def test_high_precision_amounts_keep_every_digit(led: Ledger):
    led.create_commodity("BTC", "security", 8)
    led.create_account("Assets:Wallet", "asset", JAN1)
    led.create_account("Assets:Exchange", "asset", JAN1)
    led.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 5),
            postings=[
                PostingSpec(
                    account="Assets:Wallet",
                    units=Amount.from_decimal(
                        Decimal("0.12345678"),
                        Commodity("BTC", CommodityKind.SECURITY, 8),
                    ),
                ),
                PostingSpec(account="Assets:Exchange"),
            ],
        )
    )
    text = led.export_beancount_string()
    assert "  Assets:Wallet  0.12345678 BTC" in text
    assert "  Assets:Exchange  -0.12345678 BTC" in text


def test_tags_and_links_are_printed_sorted_on_the_header(led: Ledger):
    simple_setup(led)
    record_pair(
        led,
        "-5.00",
        payee="Cafe",
        narration="lunch",
        tags={"b-tag", "a-tag"},
        links={"z-link", "a-link"},
    )
    header = '2024-01-05 * "Cafe" "lunch" #a-tag #b-tag ^a-link ^z-link'
    assert header in led.export_beancount_string()


def test_transaction_and_posting_metadata_are_printed(led: Ledger):
    simple_setup(led)
    led.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 5),
            metadata={"purpose": "team offsite", "nights": 4},
            postings=[
                PostingSpec(
                    account="Assets:Cash",
                    units=usd("-5.00"),
                    metadata={"plaid-id": "txn-00123"},
                ),
                PostingSpec(account="Expenses:Misc"),
            ],
        )
    )
    text = led.export_beancount_string()
    assert '2024-01-05 * ""\n  nights: 4\n  purpose: "team offsite"\n' in text
    assert '  Assets:Cash  -5.00 USD\n    plaid-id: "txn-00123"\n' in text


def test_metadata_beancount_cannot_parse_is_omitted(led: Ledger):
    """Only keys matching Beancount's syntax with scalar values export;
    the rest is Obol-internal (module docstring). bool is checked before
    int — TRUE, not 1 — and floats print without exponents."""
    simple_setup(led)
    led.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 5),
            metadata={
                "ok-key": "kept",
                "confirmed": True,
                "ratio": 0.25,
                "Capital": "invalid key",
                "x": "too short",
                "9lead": "invalid key",
                "filename": "reserved",
                "lineno": 7,
                "nested": {"a": 1},
                "listy": [1, 2],
                "nullish": None,
            },
            postings=[
                PostingSpec(account="Assets:Cash", units=usd("-5.00")),
                PostingSpec(account="Expenses:Misc"),
            ],
        )
    )
    text = led.export_beancount_string()
    assert '  ok-key: "kept"' in text
    assert "  confirmed: TRUE" in text
    assert "  ratio: 0.25" in text
    for absent in (
        "Capital",
        "too short",
        "9lead",
        "filename",
        "lineno",
        "nested",
        "listy",
        "nullish",
    ):
        assert absent not in text


def test_note_and_event_lines(led: Ledger):
    led.create_account("Assets:Cash", "asset", JAN1)
    led.add_note("Assets:Cash", datetime.date(2024, 2, 1), 'statement "arrived"')
    led.add_event(datetime.date(2024, 1, 15), "employer", "Hooli")
    led.add_event(datetime.date(2024, 6, 1), "employer", "")
    text = led.export_beancount_string()
    assert '2024-02-01 note Assets:Cash "statement \\"arrived\\""' in text
    assert '2024-01-15 event "employer" "Hooli"' in text
    assert '2024-06-01 event "employer" ""' in text


def test_documents_are_not_exported(led: Ledger):
    """Beancount verifies document files exist on disk at load time — a
    promise stored references cannot make — so document rows stay out of
    the export (module docstring)."""
    led.create_account("Assets:Cash", "asset", JAN1)
    led.add_document(
        "Assets:Cash",
        datetime.date(2024, 2, 1),
        "statements/2024-01.pdf",
        sha256="ab" * 32,
    )
    assert "document" not in led.export_beancount_string()
    assert "statements/2024-01.pdf" not in led.export_beancount_string()


def test_escaped_strings_round_trip_through_beancount(led: Ledger):
    from beancount.core import data

    simple_setup(led)
    record_pair(led, "-5.00", payee='Bob "The Builder"', narration="back\\slash")

    from beancount import loader

    entries, errors, _options = loader.load_string(led.export_beancount_string())
    assert not errors, [str(error) for error in errors]
    transactions = [e for e in entries if isinstance(e, data.Transaction)]
    assert len(transactions) == 1
    assert transactions[0].payee == 'Bob "The Builder"'
    assert transactions[0].narration == "back\\slash"
