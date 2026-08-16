"""Builders for the golden scenarios (design §14), shared between the
balance assertions (test_scenarios.py) and the export byte-compare and
oracle tests (test_export.py). Every asserted number in the tests that use
these was computed by hand."""

import datetime
from decimal import Decimal

from ledger.api import Ledger
from ledger.domain.amount import Amount, Commodity, CommodityKind
from ledger.domain.transaction import CostSpec, PostingSpec, TransactionSpec

USD = Commodity("USD", CommodityKind.CURRENCY, 2)
IRAUSD = Commodity("IRAUSD", CommodityKind.TRACKING, 2)
VACHR = Commodity("VACHR", CommodityKind.TRACKING, 0)
AAPL = Commodity("AAPL", CommodityKind.SECURITY, 0)


def A(text: str, commodity: Commodity = USD) -> Amount:
    return Amount.from_decimal(Decimal(text), commodity)


def leg(account: str, text: str | None, commodity: Commodity = USD) -> PostingSpec:
    units = A(text, commodity) if text is not None else None
    return PostingSpec(account=account, units=units)


def build_month_of_spending(ledger: Ledger) -> Ledger:
    """A month of ordinary expenses across a checking account and a credit
    card, ending with the card paid off in full."""
    ledger.create_commodity("USD", "currency")
    jan1 = datetime.date(2024, 1, 1)
    for path, type_ in [
        ("Assets:Checking", "asset"),
        ("Liabilities:Card", "liability"),
        ("Equity:Opening-Balances", "equity"),
        ("Expenses:Home:Rent", "expense"),
        ("Expenses:Food:Groceries", "expense"),
        ("Expenses:Food:Restaurant", "expense"),
        ("Expenses:Food:Coffee", "expense"),
    ]:
        ledger.create_account(path, type_, jan1)

    def record(day, *postings, **kwargs):
        ledger.record(
            TransactionSpec(
                date=datetime.date(2024, 1, day),
                postings=list(postings),
                **kwargs,
            )
        )

    record(
        1,
        leg("Equity:Opening-Balances", "-2500.00"),
        leg("Assets:Checking", "2500.00"),
        narration="opening balance",
    )
    record(
        2,
        leg("Assets:Checking", "-1400.00"),
        leg("Expenses:Home:Rent", None),
        payee="Landlord",
    )
    record(
        5,
        leg("Liabilities:Card", "-85.30"),
        leg("Expenses:Food:Groceries", "85.30"),
        payee="Safeway",
    )
    record(
        12,
        leg("Liabilities:Card", "-42.15"),
        leg("Expenses:Food:Restaurant", "42.15"),
        payee="Thai Palace",
    )
    record(
        14,
        leg("Assets:Checking", "-4.50"),
        leg("Expenses:Food:Coffee", "4.50"),
        payee="Blue Bottle",
    )
    record(
        28,
        leg("Assets:Checking", "-127.45"),
        leg("Liabilities:Card", "127.45"),
        narration="card autopay",
    )
    return ledger


PAYCHECK_ACCOUNTS = [
    ("Income:Hooli:Salary", "income"),
    ("Income:Hooli:Bonus", "income"),
    ("Income:Hooli:Vacation", "income"),
    ("Assets:BofA:Checking", "asset"),
    ("Assets:Vanguard:PreTax401k", "asset"),
    ("Assets:Vanguard:Match401k", "asset"),
    ("Assets:Federal:PreTax401k", "asset"),
    ("Assets:Hooli:Vacation", "asset"),
    ("Expenses:Taxes:Federal", "expense"),
    ("Expenses:Taxes:State", "expense"),
    ("Expenses:Taxes:SocSec", "expense"),
    ("Expenses:Taxes:Medicare", "expense"),
    ("Expenses:Taxes:SDI", "expense"),
    ("Expenses:Taxes:PreTax401k", "expense"),
    ("Expenses:Health:Insurance", "expense"),
    ("Expenses:Health:Dental", "expense"),
    ("Expenses:Health:Vision", "expense"),
    ("Expenses:Fees:Payroll", "expense"),
]


def build_paycheck_accounts(ledger: Ledger) -> Ledger:
    """Commodities and accounts for the 18-posting, three-commodity
    paycheck (design §4): salary, withholding, 401k in USD; the IRS
    contribution cap tracked in IRAUSD; PTO accrual in VACHR."""
    ledger.create_commodity("USD", "currency")
    ledger.create_commodity("IRAUSD", "tracking")
    ledger.create_commodity("VACHR", "tracking", 0)
    jan1 = datetime.date(2024, 1, 1)
    for path, type_ in PAYCHECK_ACCOUNTS:
        ledger.create_account(path, type_, jan1)
    return ledger


def paycheck_spec(day: int) -> TransactionSpec:
    """The checking deposit is interpolated — the exact shape of the Plaid
    pipeline."""
    return TransactionSpec(
        date=datetime.date(2024, 1, day),
        payee="Hooli",
        narration="Payroll",
        postings=[
            leg("Income:Hooli:Salary", "-4432.16"),
            leg("Income:Hooli:Bonus", "-183.22"),
            leg("Assets:BofA:Checking", None),  # -> 1350.60
            leg("Assets:Vanguard:PreTax401k", "961.54"),
            leg("Assets:Vanguard:Match401k", "238.46"),
            leg("Expenses:Taxes:Federal", "1062.92"),
            leg("Expenses:Taxes:State", "561.29"),
            leg("Expenses:Taxes:SocSec", "286.15"),
            leg("Expenses:Taxes:Medicare", "106.62"),
            leg("Expenses:Taxes:SDI", "1.12"),
            leg("Expenses:Health:Insurance", "24.32"),
            leg("Expenses:Health:Dental", "2.90"),
            leg("Expenses:Health:Vision", "18.48"),
            leg("Expenses:Fees:Payroll", "0.98"),
            leg("Assets:Federal:PreTax401k", "-1200.00", IRAUSD),
            leg("Expenses:Taxes:PreTax401k", "1200.00", IRAUSD),
            leg("Assets:Hooli:Vacation", "5", VACHR),
            leg("Income:Hooli:Vacation", "-5", VACHR),
        ],
    )


def build_one_paycheck(ledger: Ledger) -> Ledger:
    build_paycheck_accounts(ledger)
    ledger.record(paycheck_spec(15))
    return ledger


def build_midlife_connection(ledger: Ledger) -> Ledger:
    """An account connected mid-life (design §8): it existed long before
    Obol did, Plaid supplies only recent history plus a reported balance.
    Open, pad against opening balances, record what Plaid sent, then the
    first balance assertion springs the pad.

    Hand-computed: recorded activity sums to -54.23 + 2000.00 - 120.00 =
    1825.77, the first reported balance is 5000.00, so the pad books
    3174.23 from equity on the pad date. After one more purchase the
    second assertion (4939.50) passes with no pad."""
    ledger.create_commodity("USD", "currency")
    mar1 = datetime.date(2024, 3, 1)
    for path, type_ in [
        ("Assets:Chase:Checking", "asset"),
        ("Equity:Opening-Balances", "equity"),
        ("Expenses:Food:Groceries", "expense"),
        ("Expenses:Utilities", "expense"),
        ("Income:Employer:Salary", "income"),
    ]:
        ledger.create_account(path, type_, mar1)

    ledger.pad("Assets:Chase:Checking", "Equity:Opening-Balances", mar1)

    def record(day, *postings, **kwargs):
        ledger.record(
            TransactionSpec(
                date=datetime.date(2024, 3, day),
                postings=list(postings),
                **kwargs,
            )
        )

    record(
        2,
        leg("Assets:Chase:Checking", "-54.23"),
        leg("Expenses:Food:Groceries", None),
        payee="Safeway",
    )
    record(
        5,
        leg("Assets:Chase:Checking", "2000.00"),
        leg("Income:Employer:Salary", None),
        payee="Employer",
        narration="Payroll",
    )
    record(
        7,
        leg("Assets:Chase:Checking", "-120.00"),
        leg("Expenses:Utilities", None),
        payee="City Power",
    )

    ledger.assert_balance(
        "Assets:Chase:Checking",
        datetime.date(2024, 3, 10),
        Decimal("5000.00"),
        "USD",
        source="plaid",
    )

    record(
        12,
        leg("Assets:Chase:Checking", "-60.50"),
        leg("Expenses:Food:Groceries", None),
        payee="Safeway",
    )
    ledger.assert_balance(
        "Assets:Chase:Checking",
        datetime.date(2024, 3, 15),
        Decimal("4939.50"),
        "USD",
        source="plaid",
    )
    return ledger


def build_tagged_trip(ledger: Ledger) -> Ledger:
    """M6: a trip sliced by tag across account boundaries — spending paid
    from the card and from checking, across Travel and Food — plus the
    hub attachments: a note and a document on the card, location events.

    Hand-computed: tagged spending is 450.00 + 620.00 + 84.60 + 6.25 =
    1160.85 USD (90.85 of it food); the 52.40 restaurant visit after the
    trip is untagged and stays outside the slice. The document must not
    appear in the export (Beancount verifies document files exist)."""
    ledger.create_commodity("USD", "currency")
    may1 = datetime.date(2024, 5, 1)
    for path, type_ in [
        ("Assets:Checking", "asset"),
        ("Liabilities:Card", "liability"),
        ("Equity:Opening-Balances", "equity"),
        ("Expenses:Travel:Flights", "expense"),
        ("Expenses:Travel:Hotel", "expense"),
        ("Expenses:Food:Restaurant", "expense"),
        ("Expenses:Food:Coffee", "expense"),
    ]:
        ledger.create_account(path, type_, may1)

    ledger.record(
        TransactionSpec(
            date=may1,
            narration="opening balance",
            postings=[
                leg("Equity:Opening-Balances", "-3000.00"),
                leg("Assets:Checking", "3000.00"),
            ],
        )
    )

    def record(day, *postings, **kwargs):
        ledger.record(
            TransactionSpec(
                date=datetime.date(2024, 6, day),
                postings=list(postings),
                **kwargs,
            )
        )

    record(
        1,
        PostingSpec(
            account="Liabilities:Card",
            units=A("-450.00"),
            metadata={"plaid-id": "plaid-txn-081"},
        ),
        leg("Expenses:Travel:Flights", None),
        payee="Delta",
        tags={"trip-nyc-2024"},
        links={"nyc-itinerary"},
        metadata={"booking-ref": "XK4J9"},
    )
    record(
        12,
        leg("Liabilities:Card", "-620.00"),
        leg("Expenses:Travel:Hotel", None),
        payee="Hotel Chelsea",
        tags={"trip-nyc-2024"},
        links={"nyc-itinerary"},
        metadata={"nights": 4},
    )
    record(
        13,
        leg("Assets:Checking", "-84.60"),
        leg("Expenses:Food:Restaurant", None),
        payee="Katz's",
        tags={"trip-nyc-2024"},
    )
    record(
        13,
        leg("Assets:Checking", "-6.25"),
        leg("Expenses:Food:Coffee", None),
        payee="Blue Bottle",
        tags={"trip-nyc-2024"},
    )
    record(
        20,
        leg("Assets:Checking", "-52.40"),
        leg("Expenses:Food:Restaurant", None),
        payee="Thai Palace",
    )

    ledger.add_note(
        "Liabilities:Card",
        datetime.date(2024, 6, 15),
        "Card statement includes the NYC trip charges",
    )
    ledger.add_document(
        "Liabilities:Card",
        datetime.date(2024, 6, 30),
        "statements/2024-06.pdf",
        sha256="ab" * 32,
    )
    ledger.add_event(datetime.date(2024, 6, 10), "location", "New York, NY")
    ledger.add_event(datetime.date(2024, 6, 14), "location", "Boston, MA")
    return ledger


def build_stock_sale(ledger: Ledger) -> Ledger:
    """A stock purchase, a second lot, a partial FIFO sale with a
    commission, and the resulting realized gain (design §14).

    Hand-computed: FIFO takes all 8 of the 156.25 lot (1250.00) plus 2 of
    the 172.00 lot (344.00) — cost basis 1594.00. Proceeds 10 x 189.10 =
    1891.00, commission 8.95, cash receives 1882.05. The interpolated
    gains leg is -(−1594.00 + 1882.05 + 8.95) = −297.00: a realized gain
    of 297.00 with the commission expensed separately. Remaining: 5 AAPL
    at 172.00; at the 2024-03-28 closing price 190.50, market value is
    952.50 and unrealized gain 5 x 18.50 = 92.50."""
    ledger.create_commodity("AAPL", "security", 0)
    ledger.create_commodity("USD", "currency")
    ledger.set_option("operating_currency", "USD")
    ledger.set_option("gains_account_root", "Income:Gains")
    jan2 = datetime.date(2024, 1, 2)
    ledger.create_account("Assets:ETrade:AAPL", "asset", jan2, booking_method="FIFO")
    ledger.create_account("Assets:ETrade:Cash", "asset", jan2)
    ledger.create_account("Equity:Opening-Balances", "equity", jan2)
    ledger.create_account("Expenses:Financial:Commissions", "expense", jan2)

    def record(month, day, *postings, **kwargs):
        ledger.record(
            TransactionSpec(
                date=datetime.date(2024, month, day),
                postings=list(postings),
                **kwargs,
            )
        )

    record(
        1,
        2,
        leg("Assets:ETrade:Cash", "10000.00"),
        leg("Equity:Opening-Balances", "-10000.00"),
        narration="opening balance",
    )
    record(
        1,
        5,
        PostingSpec(
            account="Assets:ETrade:AAPL",
            units=A("8", AAPL),
            cost=CostSpec(per_unit=Decimal("156.25"), commodity="USD"),
        ),
        leg("Assets:ETrade:Cash", "-1250.00"),
        narration="Buy 8 AAPL",
    )
    record(
        2,
        9,
        PostingSpec(
            account="Assets:ETrade:AAPL",
            units=A("7", AAPL),
            cost=CostSpec(per_unit=Decimal("172.00"), commodity="USD"),
        ),
        leg("Assets:ETrade:Cash", "-1204.00"),
        narration="Buy 7 AAPL",
    )
    record(
        3,
        18,
        PostingSpec(
            account="Assets:ETrade:AAPL",
            units=A("-10", AAPL),
            cost=CostSpec(),
            price=A("189.10"),
        ),
        leg("Assets:ETrade:Cash", "1882.05"),
        leg("Expenses:Financial:Commissions", "8.95"),
        leg("Income:Gains:ETrade", None),  # -> interpolated -297.00
        narration="Sell 10 AAPL",
    )
    ledger.record_price("AAPL", datetime.date(2024, 3, 28), Decimal("190.50"), "USD")
    return ledger


def build_transfer(ledger: Ledger) -> Ledger:
    """A transfer between own accounts: value moves, net worth does not."""
    ledger.create_commodity("USD", "currency")
    jan1 = datetime.date(2024, 1, 1)
    ledger.create_account("Assets:Checking", "asset", jan1)
    ledger.create_account("Assets:Savings", "asset", jan1)
    ledger.create_account("Equity:Opening-Balances", "equity", jan1)
    ledger.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 1),
            postings=[
                leg("Equity:Opening-Balances", "-1000.00"),
                leg("Assets:Checking", "1000.00"),
            ],
        )
    )
    ledger.record(
        TransactionSpec(
            date=datetime.date(2024, 1, 15),
            narration="move to savings",
            postings=[
                leg("Assets:Checking", "-250.00"),
                leg("Assets:Savings", None),
            ],
        )
    )
    return ledger
