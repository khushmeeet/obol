"""M6: tags, links, and the hub attachments, on both backends (plan §8).

The exit criterion lives here: a query slices spending by tag across
account boundaries — different expense categories, paid from different
accounts — and matches a hand-computed answer.
"""

import datetime
from decimal import Decimal

import pytest

from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.errors import (
    AccountNotOpenError,
    DocumentError,
    EventError,
    InvalidLinkError,
    InvalidTagError,
    NoteError,
    UnknownAccountError,
)
from ledger.domain.transaction import PostingSpec, TransactionSpec

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
MAY1 = datetime.date(2024, 5, 1)
TRIP = "trip-nyc-2024"


def usd(text: str) -> Amount:
    return Amount.from_decimal(Decimal(text), USD)


def leg(account: str, text: str | None = None) -> PostingSpec:
    return PostingSpec(account=account, units=usd(text) if text else None)


@pytest.fixture
def led(ledger):
    ledger.create_commodity("USD", "currency")
    for path, type_ in [
        ("Assets:Checking", "asset"),
        ("Liabilities:Card", "liability"),
        ("Equity:Opening-Balances", "equity"),
        ("Expenses:Travel:Flights", "expense"),
        ("Expenses:Travel:Hotel", "expense"),
        ("Expenses:Food:Restaurant", "expense"),
        ("Expenses:Food:Coffee", "expense"),
    ]:
        ledger.create_account(path, type_, MAY1)
    return ledger


def record(led, day, postings, **kwargs):
    return led.record(
        TransactionSpec(date=datetime.date(2024, 6, day), postings=postings, **kwargs)
    )


def build_trip(led):
    """Four tagged transactions across two payment accounts and four
    expense accounts, plus one untagged control. Tagged spending:
    450.00 + 620.00 + 84.60 + 6.25 = 1160.85 USD."""
    led.record(
        TransactionSpec(
            date=MAY1,
            postings=[
                leg("Equity:Opening-Balances", "-3000.00"),
                leg("Assets:Checking", "3000.00"),
            ],
        )
    )
    record(
        led,
        1,
        [leg("Liabilities:Card", "-450.00"), leg("Expenses:Travel:Flights")],
        payee="Delta",
        tags={TRIP},
        links={"nyc-itinerary"},
    )
    record(
        led,
        12,
        [leg("Liabilities:Card", "-620.00"), leg("Expenses:Travel:Hotel")],
        payee="Hotel Chelsea",
        tags={TRIP},
        links={"nyc-itinerary"},
    )
    record(
        led,
        13,
        [leg("Assets:Checking", "-84.60"), leg("Expenses:Food:Restaurant")],
        payee="Katz's",
        tags={TRIP},
    )
    record(
        led,
        13,
        [leg("Assets:Checking", "-6.25"), leg("Expenses:Food:Coffee")],
        payee="Blue Bottle",
        tags={TRIP},
    )
    record(
        led,
        20,
        [leg("Assets:Checking", "-52.40"), leg("Expenses:Food:Restaurant")],
        payee="Thai Palace",
    )
    return led


def total(entries) -> Decimal:
    return sum((entry.posting.units.to_decimal() for entry in entries), Decimal(0))


# --- tags and links on transactions ----------------------------------------


def test_tags_and_links_round_trip(led):
    recorded = record(
        led,
        1,
        [leg("Assets:Checking", "-10.00"), leg("Expenses:Food:Coffee")],
        tags={"b", "a"},
        links={"conf-42"},
        source="plaid",
        source_ref="txn-1",
    )
    assert recorded.tags == frozenset({"a", "b"})
    assert recorded.links == frozenset({"conf-42"})
    fetched = led.get_transaction(recorded.id)
    assert fetched.tags == frozenset({"a", "b"})
    assert fetched.links == frozenset({"conf-42"})
    by_source = led.get_transaction_by_source("plaid", "txn-1")
    assert by_source.tags == frozenset({"a", "b"})
    listed = led.list_transactions()
    assert [t.tags for t in listed] == [frozenset({"a", "b"})]


def test_untagged_transactions_carry_empty_sets(led):
    recorded = record(
        led, 1, [leg("Assets:Checking", "-10.00"), leg("Expenses:Food:Coffee")]
    )
    fetched = led.get_transaction(recorded.id)
    assert fetched.tags == frozenset()
    assert fetched.links == frozenset()


@pytest.mark.parametrize("bad", ["", "with space", "né", "a:b"])
def test_invalid_tag_or_link_is_refused_and_nothing_is_written(led, bad):
    with pytest.raises(InvalidTagError):
        record(
            led,
            1,
            [leg("Assets:Checking", "-10.00"), leg("Expenses:Food:Coffee")],
            tags={bad},
        )
    with pytest.raises(InvalidLinkError):
        record(
            led,
            1,
            [leg("Assets:Checking", "-10.00"), leg("Expenses:Food:Coffee")],
            links={bad},
        )
    assert led.list_transactions() == []


def test_a_tag_name_is_shared_across_transactions(led):
    build_trip(led)
    if led._conn is not None:  # SQLite: one tags row serves all four uses
        count = led._conn.execute(
            "SELECT COUNT(*) FROM tags WHERE name = ?", (TRIP,)
        ).fetchone()[0]
        assert count == 1


# --- slicing by tag and link ------------------------------------------------


def test_list_transactions_filters_by_tag_and_link(led):
    build_trip(led)
    assert len(led.list_transactions()) == 6
    tagged = led.list_transactions(tag=TRIP)
    assert [t.payee for t in tagged] == [
        "Delta",
        "Hotel Chelsea",
        "Katz's",
        "Blue Bottle",
    ]
    linked = led.list_transactions(link="nyc-itinerary")
    assert [t.payee for t in linked] == ["Delta", "Hotel Chelsea"]
    both = led.list_transactions(tag=TRIP, link="nyc-itinerary")
    assert [t.payee for t in both] == ["Delta", "Hotel Chelsea"]
    assert led.list_transactions(tag="no-such-tag") == []
    assert led.list_transactions(link="no-such-link") == []


def test_spending_by_tag_slices_across_account_boundaries(led):
    """The M6 exit criterion. The tag groups spending paid from the card
    *and* from checking, across Travel *and* Food categories; the
    untagged restaurant visit stays out. Every number hand-computed."""
    build_trip(led)
    trip_spending = led.journal("Expenses", tag=TRIP)
    assert total(trip_spending) == Decimal("1160.85")
    assert {entry.posting.account for entry in trip_spending} == {
        "Expenses:Travel:Flights",
        "Expenses:Travel:Hotel",
        "Expenses:Food:Restaurant",
        "Expenses:Food:Coffee",
    }
    # Scoped to one subtree: food on the trip was 84.60 + 6.25.
    assert total(led.journal("Expenses:Food", tag=TRIP)) == Decimal("90.85")
    # The paying side of the same slice: card charges on the trip.
    assert total(led.journal("Liabilities:Card", tag=TRIP)) == Decimal("-1070.00")
    # All expenses, tagged or not: 1160.85 + 52.40.
    assert total(led.journal("Expenses")) == Decimal("1213.25")


def test_journal_combines_tag_with_dates_and_children(led):
    build_trip(led)
    june13 = datetime.date(2024, 6, 13)
    assert total(led.journal("Expenses", june13, june13, tag=TRIP)) == Decimal("90.85")
    assert led.journal("Expenses:Food:Coffee", include_children=False, tag=TRIP)[
        0
    ].posting.units.to_decimal() == Decimal("6.25")
    assert led.journal("Expenses", tag=TRIP, link="nyc-itinerary") == [
        entry for entry in led.journal("Expenses", link="nyc-itinerary")
    ]


def test_reversal_carries_the_tags_so_the_slice_cancels(led):
    build_trip(led)
    hotel = led.list_transactions(tag=TRIP)[1]
    assert hotel.payee == "Hotel Chelsea"
    reversal = led.reverse(hotel.id, datetime.date(2024, 6, 25), "double charge")
    assert reversal.tags == frozenset({TRIP})
    assert reversal.links == frozenset({"nyc-itinerary"})
    assert total(led.journal("Expenses", tag=TRIP)) == Decimal("540.85")
    assert led.validate().ok


# --- notes ------------------------------------------------------------------


def test_notes_round_trip_ordered_and_filtered(led):
    led.add_note("Liabilities:Card", datetime.date(2024, 6, 15), "NYC charges")
    led.add_note("Assets:Checking", datetime.date(2024, 6, 1), "switched plan")
    notes = led.list_notes()
    assert [(n.date.day, n.account, n.comment) for n in notes] == [
        (1, "Assets:Checking", "switched plan"),
        (15, "Liabilities:Card", "NYC charges"),
    ]
    assert all(n.id is not None for n in notes)
    only_card = led.list_notes("Liabilities:Card")
    assert [n.comment for n in only_card] == ["NYC charges"]
    assert led.list_notes("Expenses:Food:Coffee") == []


def test_note_guards(led):
    with pytest.raises(UnknownAccountError):
        led.add_note("Assets:Nowhere", MAY1, "hello")
    with pytest.raises(AccountNotOpenError):
        led.add_note("Assets:Checking", datetime.date(2024, 4, 30), "too early")
    with pytest.raises(NoteError):
        led.add_note("Assets:Checking", MAY1, "   ")
    assert led.list_notes() == []


def test_note_after_close_is_allowed(led):
    """Same rule as balance assertions (M4, verified against Beancount):
    dating before open is an error, dating after close is not."""
    led.close_account("Expenses:Food:Coffee", datetime.date(2024, 6, 30))
    note = led.add_note(
        "Expenses:Food:Coffee", datetime.date(2024, 7, 5), "closed after trip"
    )
    assert note.id is not None


# --- documents --------------------------------------------------------------


def test_documents_round_trip_and_sha_normalization(led):
    led.add_document(
        "Liabilities:Card",
        datetime.date(2024, 6, 30),
        "statements/2024-06.pdf",
        sha256="AB" * 32,
    )
    led.add_document(
        "Liabilities:Card", datetime.date(2024, 5, 31), "statements/2024-05.pdf"
    )
    documents = led.list_documents("Liabilities:Card")
    assert [d.path for d in documents] == [
        "statements/2024-05.pdf",
        "statements/2024-06.pdf",
    ]
    assert documents[0].sha256 is None
    assert documents[1].sha256 == "ab" * 32  # stored lowercase
    assert led.list_documents("Assets:Checking") == []


def test_document_guards(led):
    with pytest.raises(UnknownAccountError):
        led.add_document("Assets:Nowhere", MAY1, "x.pdf")
    with pytest.raises(AccountNotOpenError):
        led.add_document("Assets:Checking", datetime.date(2024, 4, 30), "x.pdf")
    with pytest.raises(DocumentError):
        led.add_document("Assets:Checking", MAY1, "   ")
    with pytest.raises(DocumentError):
        led.add_document("Assets:Checking", MAY1, "x.pdf", sha256="not-hex")
    with pytest.raises(DocumentError):
        led.add_document("Assets:Checking", MAY1, "x.pdf", sha256="ab" * 31)
    assert led.list_documents() == []


# --- events -----------------------------------------------------------------


def test_events_round_trip_and_type_filter(led):
    led.add_event(datetime.date(2024, 6, 10), "location", "New York, NY")
    led.add_event(datetime.date(2024, 6, 14), "location", "Boston, MA")
    led.add_event(datetime.date(2024, 6, 1), "employer", "Hooli")
    assert [(e.date.day, e.type, e.value) for e in led.list_events()] == [
        (1, "employer", "Hooli"),
        (10, "location", "New York, NY"),
        (14, "location", "Boston, MA"),
    ]
    assert [e.value for e in led.list_events("location")] == [
        "New York, NY",
        "Boston, MA",
    ]
    assert led.list_events("address") == []


def test_event_guards(led):
    with pytest.raises(EventError):
        led.add_event(MAY1, "  ", "value")
    empty_value = led.add_event(MAY1, "employer", "")  # allowed (Beancount too)
    assert empty_value.id is not None


# --- the whole hub stays validator-clean ------------------------------------


def test_hub_data_keeps_validation_clean(led, assert_matches_beancount):
    build_trip(led)
    led.add_note("Liabilities:Card", datetime.date(2024, 6, 15), "NYC charges")
    led.add_document(
        "Liabilities:Card",
        datetime.date(2024, 6, 30),
        "statements/2024-06.pdf",
        sha256="ab" * 32,
    )
    led.add_event(datetime.date(2024, 6, 10), "location", "New York, NY")
    assert led.validate().ok
    assert_matches_beancount(led)
