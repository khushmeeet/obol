# ledger

A product-independent, double-entry accounting engine in Python on SQLite.
It records and queries financial data; it knows nothing about Plaid,
budgets, categorization, or HTTP. See `obol-ledger-design.md` (the what),
`obol-ledger-implementation-plan.md` (the how), and
`obol-ledger-progress.md` (what has landed) at the repository root.

## Status

**M1 — walking skeleton — complete.** Commodities, accounts, transactions,
postings, per-commodity balancing with inferred tolerances, interpolation,
`record()`, `balance()`, `journal()`. Two storage backends (SQLite and
in-memory) run the same test suite.

**M2 — Beancount export and oracle harness — complete.**
`export_beancount()` / `export_beancount_string()` serialize the whole
ledger to precision-faithful Beancount text via a deliberately dumb
serializer (`interop/export.py`). Every golden scenario is byte-compared
against hand-written expected output, loads cleanly under a pinned
`beancount==3.2.3`, passes `bean-check`, and matches Beancount's computed
balances exactly. The `assert_matches_beancount` oracle fixture is
available to every test from here on.

**M3 — hierarchy and statements — complete.** `balance_sheet(on)` (net
worth is one call) and `income_statement(start, end)` (category breakdown
with drill-down), built as trees of prefix rollups. Sign flipping for
display lives in `query/statements.py` and nowhere else.

**M4 — assertions, padding, validation — complete.** `assert_balance()`
stores and immediately checks a balance assertion (start-of-date,
sub-accounts included, tolerance from the asserted precision — Beancount's
verified rules); a failure is data, not an exception. `pad()` arms the
mid-life account connection: the next assertion books the difference from
equity in a generated, flagged transaction. `validate()` runs every
integrity check and returns a structured report; the `ledger validate`
CLI drives it.

**M5 — corrections — complete.** `reverse()` posts the exact negation of
a committed transaction, dated on the date of discovery (never backdated)
and linked via `reverses_id`; `replace()` does that plus records the
corrected transaction in one atomic write — the Plaid pending→posted
revision shape. Originals stay queryable forever and keep their
`(source, source_ref)` dedup key; `get_transaction_by_source()` finds a
transaction by its ingestion ref (or a reversal by its original's id).
`BEFORE UPDATE`/`BEFORE DELETE` triggers make transactions and postings
append-only at the database level.

**M6 — tags, links, and the hub attachments — complete.** Transactions
carry `tags` and `links` (Beancount's charset, validated at record time,
append-only with the rest of the record); `list_transactions(tag=...)`
and `journal(account, tag=...)` slice across account boundaries — one
call answers "what did the NYC trip cost, whichever account paid".
Reversals carry the original's tags so corrections cancel inside the
slice. `add_note()` / `add_document()` / `add_event()` store the
hub attachments: dated comments and file references (path + SHA-256) on
accounts, and ledger-wide dated facts. The exporter now emits
`#tag`/`^link`, transaction and posting metadata, `note`, and `event`
(documents deliberately stay internal — Beancount verifies the files
exist on disk).

**M7 — cost, lots, booking, and gains — complete.** Postings carry cost
(`{...}`, the lot identity that determines weight) and price (`@ ...`,
the exchange rate that does not). Acquisitions open stored lots;
reductions are booked at write time — STRICT / FIFO / LIFO / SPECIFIC —
and the matches recorded, so cost basis is deterministic and auditable.
The realized-gain posting rides the existing interpolation: leave the
gains leg open and the ledger fills it. `inventory()` exposes holdings
lot by lot; the price table (`record_price()`, implied observations from
transactions) drives `market_value()` and `unrealized_gain()`. Reversals
restore lots with compensating rows; nothing is ever deleted.

**M8 — the corpus gate — complete.** `import_beancount()` replays a
`.beancount` file onto the public API (`interop/import_.py`, loader
output mapped to `Ledger` calls — dev-only test infrastructure). Five
committed 11-year `bean-example` corpora (~4,200 transactions each)
import on both backends with zero errors and match Beancount **exactly**
— final balances, lot-level inventories, realized gains, market values
at sampled dates — then validate clean, export, reload under Beancount,
and still match. Differential fuzzing (random FIFO trading sessions with
interpolated gains legs and self-asserted balances) runs on every commit
and at a larger budget nightly. The accounting core is done; the product
can trust it with real money.

## Quick taste

```python
import datetime
from decimal import Decimal

from ledger import Amount, Ledger, PostingSpec, TransactionSpec

led = Ledger.open("obol.db")
usd = led.create_commodity("USD", "currency")
led.create_account("Assets:Checking", "asset", datetime.date(2024, 1, 1))
led.create_account("Expenses:Food", "expense", datetime.date(2024, 1, 1))

led.record(
    TransactionSpec(
        date=datetime.date(2024, 1, 10),
        payee="Corner Shop",
        postings=[
            PostingSpec("Assets:Checking", Amount.from_decimal(Decimal("-42.50"), usd)),
            PostingSpec("Expenses:Food"),  # interpolated to 42.50 USD
        ],
    )
)

led.balance("Assets:Checking").to_dict()  # {'USD': Decimal('-42.50')}
```

## Development

```sh
uv sync                 # from the workspace root
just test               # or: cd ledger && uv run pytest
just check              # lint + format + mypy --strict (domain) + tests
just corpus             # the M8 corpus gate (pytest -m corpus)
just fuzz               # differential fuzzing, larger example budget
just corpus-fixtures    # regenerate fixtures (only after a Beancount upgrade)
```

Runtime dependencies: none (stdlib only). `beancount==3.2.3` is pinned as
a dev-only test oracle; if it ever appears in the runtime dependency set,
something has gone wrong architecturally.
