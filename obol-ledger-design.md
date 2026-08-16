# Obol Ledger — Design for the Accounting Core

A product-independent, double-entry accounting engine in Python on SQLite.
Built once, correctly, to outlive every product feature layered on top of it.

**Revision 2.** This draft has been checked against Beancount 3.2.3 running
locally on a `bean-example` generated ledger (2,005 directives, 1,028
transactions, 2.5 years). Several decisions in revision 1 were wrong and are
corrected here; §17 lists them explicitly.

---

## 1. Purpose and boundaries

`ledger` is a standalone Python package that records and queries
double-entry financial data. It knows nothing about Plaid, budgets,
categorization rules, review queues, pay cycles, or HTTP.

**Inside the library**

- Commodities (currencies, securities, and non-monetary tracking units)
- Accounts, their types, hierarchy, and lifetime
- Transactions and postings
- The balancing invariant, tolerances, and interpolation
- Cost basis, lots, and booking methods
- Realized gain postings
- Balance assertions and padding
- Prices and market valuation
- Whole-ledger validation
- Balance, journal, and statement queries

**Outside the library (product concerns)**

- Plaid ingestion, sync cursors, raw payload storage
- Categorization rules and the review queue
- Pay-cycle period definitions
- Subscription detection
- Web API, auth, UI

The test: **would an accountant recognise it as accounting?** Guessing a
category is not accounting. Enforcing that a transaction balances is.

The product imports `ledger.api` and nothing else. It never writes SQL,
never inserts a posting outside the API, never mutates ledger rows.

---

## 2. Design principles

1. **Postings are immutable.** No updates, no deletes. Corrections are new
   transactions that reverse and re-post.
2. **Balances are derived.** Any balance is a query over postings. Caches are
   rebuildable from postings alone, and a test asserts they always are.
3. **Every transaction balances to zero, per commodity, within tolerance.**
4. **Amounts are exact.** Integer arithmetic internally, `Decimal` at the
   boundary. Floats never touch money.
5. **The domain layer is pure.** No I/O, no SQL, no framework.
6. **Commodity-aware from day one.** USD is not special-cased.
7. **Everything is auditable.** Every displayed number traces to postings, and
   every posting traces to the source that created it.
8. **Beancount is the reference implementation.** Where its semantics are
   reasonable, adopt them exactly, so it can serve as a test oracle. Where we
   diverge, do so deliberately and write it down (§16).

---

## 3. Package layout

```
src/ledger/
  domain/            pure, no I/O, fully unit-testable
    amount.py        Amount, Commodity, precision & rounding
    accounts.py      Account, AccountType, path handling
    transaction.py   Transaction, Posting, TransactionSpec
    inventory.py     Inventory, Lot, position arithmetic
    booking.py       STRICT / FIFO / LIFO / SPECIFIC / NONE resolution
    balancing.py     weights, tolerance inference, interpolation
    errors.py        typed exceptions

  storage/
    schema.sql       DDL
    migrations/      0001_init.sql, ...
    db.py            connection, pragmas, unit-of-work
    repositories.py  row <-> domain mapping

  validation/
    checks.py        individual invariant checks
    validator.py     run all, return structured report

  query/
    balances.py      balance / inventory at a date
    journal.py       posting register
    statements.py    balance sheet, income statement
    valuation.py     market value from the price table

  interop/
    export.py        ledger -> .beancount text
    import_.py       .beancount text -> ledger (for corpus testing)

  api.py             Ledger facade — the only public surface
```

---

## 4. Core model

### Commodity

A unit that amounts are denominated in. Three kinds, and the third matters
more than it looks:

- **currency** — `USD`, `EUR`
- **security** — `AAPL`, `VTSAX`, `VBMPX`
- **tracking** — non-monetary units used to count things that aren't money

The tracking kind is a Beancount idiom worth adopting deliberately. The
generated example ledger uses `VACHR` for employer vacation hours and
`IRAUSD` for 401k contribution tracking — units that flow through real
double-entry transactions but have no market value:

```
2024-01-04 * "Hooli" "Payroll"
  Assets:US:BofA:Checking            1350.60 USD
  Income:US:Hooli:Salary            -4615.38 USD
  Expenses:Taxes:Y2024:US:Federal    1062.92 USD
  ...
  Assets:US:Federal:PreTax401k      -1200.00 IRAUSD
  Expenses:Taxes:Y2024:US:Federal:PreTax401k  1200.00 IRAUSD
  Assets:US:Hooli:Vacation                 5 VACHR
  Income:US:Hooli:Vacation                -5 VACHR
```

One transaction, three commodities, each balancing independently. This is
exactly how a 401k contribution limit gets tracked against the IRS annual cap,
and how PTO accrual and use get tracked in the same system as money. For
"record as much financial data as possible," this is the mechanism.

Commodity fields: `symbol`, `name`, `kind`, `display_precision`, `first_date`,
`metadata` (ticker for export, price source, notes).

### Account

- **Type**: `ASSET`, `LIABILITY`, `EQUITY`, `INCOME`, `EXPENSE`. Type drives
  every report.
- **Path**: colon-separated hierarchy — `Assets:Banking:Chase:Checking`.
  Rollups are prefix matches. The tree *is* the taxonomy.
- **Open / close dates**: postings outside the lifetime are invalid.
- **Allowed commodities** (optional): in the example ledger, 45 of 63 accounts
  carry this constraint. It is a cheap, effective guardrail.
- **Booking method**: per-account, defaulting to the ledger-wide default.
- **Metadata**: free-form, uninterpreted by the library (Plaid account id,
  institution, mask).

### Transaction

`date`, `flag`, `payee`, `narration`, `tags`, `links`, `metadata`, `source`,
`source_ref`, `created_at`, `reverses_id`.

Transactions are not two-legged. In the example ledger, 921 of 1,028 have two
postings — but 48 have **eighteen** (paychecks) and 21 have fifteen. Design and
test for the many-posting case from the start.

### Posting

`account`, `units` (amount + commodity), optional `cost`, optional `price`,
optional `flag`, `lot_id`, `weight`, `metadata`.

**`cost` and `price` mean different things.** `cost` identifies the lot being
held or reduced and determines the posting's weight. `price` records the
exchange rate at which the transaction happened and does *not* affect the
weight when a cost is present. A sale carries both:

```
Assets:US:ETrade:VEA   -11 VEA {169.83 USD, 2024-09-18} @ 174.90 USD
```

`{169.83 USD, 2024-09-18}` names the lot; `@ 174.90 USD` is what it sold for.
The weight is −11 × 169.83 = −1868.13 USD.

### Sign convention

**Signed amounts. No debit/credit columns.** Positive means value flowing into
the account. Assets and Expenses hold positive balances normally; Liabilities,
Income and Equity hold negative. Reports flip signs for display. Write it down
once, apply it everywhere, never negotiate with it.

---

## 5. Money representation

**Decision: one global scale of 8 decimal places for all stored amounts,
plus a per-amount recorded precision.**

Revision 1 proposed scaling each amount by its commodity's precision. That
breaks: `VBMPX` holds 3 decimals as a quantity, but its *price* in USD needs
more, and a cost-per-unit may need more still. Precision is a property of the
number, not solely of the commodity.

So: every stored amount is an `INTEGER` at scale 8. `$50.00` is
`5_000_000_000`. `1.5` shares is `150_000_000`. All arithmetic is integer
arithmetic at a common scale, which removes an entire class of conversion bugs.

Range: int64 at scale 8 covers ±92 billion units. Ample.

Alongside each amount, store `precision` — the number of decimal places as
originally written. This is needed for two things: **tolerance inference**
(§6) and faithful display. Beancount derives both from the `Decimal` exponent;
we store it explicitly because SQLite integers don't carry one.

`Decimal` at the API boundary, converted in exactly one place
(`Amount.from_decimal` / `Amount.to_decimal`).

**Rounding policy:** intermediate products (quantity × cost) are computed at
scale 16 internally and rounded half-even to scale 8 on storage. Half-even
(banker's rounding) matches `Decimal`'s default and avoids the upward bias of
half-up across large numbers of transactions.

---

## 6. Balancing, tolerance, and interpolation

### Weights

- No cost, no price → weight = units
- Cost present → weight = quantity × cost_per_unit, in the cost commodity
- Price present, no cost → weight = quantity × price, in the price commodity

A transaction balances when, **for each commodity independently, the sum of
weights is zero within that commodity's tolerance.** Verified: a transaction
where USD balances but a tracking commodity does not is rejected.

This one mechanism covers ordinary expenses, transfers, credit-card payments,
FX, multi-commodity paychecks, and securities trades. Nothing is a special
case — which is why phase 2 does not require a rewrite.

### Tolerance inference

Revision 1 said "half the smallest unit times the number of postings." That
was hand-waving. Beancount's actual rule, confirmed by inspecting inferred
tolerances on the generated ledger:

**Tolerance is inferred per transaction, per commodity, from the decimal
precision of the numbers actually written in that transaction.** A transaction
whose USD amounts have two decimals gets `USD: 0.005`. One that also touches a
three-decimal fund gets `{'USD': 0.005, 'VBMPX': 0.0005}`. Half of the smallest
written unit.

Adopt this rule exactly. It means precision is inferred from data rather than
configured, and it degrades gracefully when a source reports more decimals.

Two refinements to carry over:
- Cost-carrying postings contribute their *cost* commodity's precision, not the
  quantity's.
- A ledger-wide `inferred_tolerance_multiplier` (default 1.0) allows loosening
  without touching the rule.

Never allow a silent plug. If a transaction does not balance within tolerance,
it is rejected — the caller must add the posting that accounts for the
difference.

### Interpolation

Beancount permits exactly one posting per commodity to omit its amount; the
value is inferred so the transaction balances. Verified:

```
2024-01-02 * "test"
  Assets:Cash   -50.00 USD
  Expenses:Food              -> inferred as 50.00 USD
```

**Adopt this.** It is not sugar — it is the natural shape of the Plaid
pipeline. Plaid supplies the bank leg with an exact amount; the category leg is
whatever balances it. The product constructs a spec with one open posting and
the ledger completes it, which removes a class of rounding errors that arise
from the product computing the counter-amount itself.

Rule: at most one posting with a missing amount per commodity per transaction;
more than one is an error.

---

## 7. Cost, lots, and booking

### Lots

A lot is a parcel of a commodity acquired at a specific cost on a specific
date. Ten AAPL at $150 and five at $180 is two lots, not fifteen shares at an
average — because selling has different tax consequences depending on which.

**Acquisition**: a posting with a positive quantity and a cost creates a lot.

**Reduction**: a negative posting must be matched against existing lots. Which
lots is decided by the booking method.

### Booking methods

- `STRICT` — the reduction must identify exactly one lot unambiguously; error
  otherwise. **This is Beancount's default**, confirmed on the loaded ledger
  (`Booking.STRICT`).
- `FIFO` — oldest first
- `LIFO` — newest first
- `SPECIFIC` — caller names the lot
- `NONE` — no matching; reductions simply add a negative position

**Decision, revised:** default to `STRICT` ledger-wide, matching Beancount, and
set `FIFO` per-account on brokerage and retirement accounts. STRICT is the
right default for a system fed by an importer, because an ambiguous reduction
is a signal that the import is missing information — it should surface as an
error to be resolved, not be silently resolved by a policy. Investment accounts
where lot-level detail is genuinely unavailable get FIFO explicitly.

`AVERAGE` is deliberately excluded: it is unimplemented in Beancount 3.2.3
(the code path raises "AVERAGE method is not supported"), so there would be no
oracle to test against. If it is ever needed, it must be built with its own
independent test basis.

### Booking is resolved at write time and stored

The reduction posting records which lot ids it drew from and how much of each,
in `lot_reductions`. Beancount recomputes booking on every parse; for a
database-backed ledger, storing it makes cost basis deterministic, auditable,
and immune to a later policy change silently rewriting history.

### Realized gain is a posting, not a query

**This is the most important correction to revision 1**, which claimed realized
gain was computed at query time from recorded data. It is not — it cannot be,
because the transaction would not balance without it.

Verified with FIFO booking:

```
2024-01-04 * "sell"
  Assets:Stock   -5 AAPL {} @ 150.00 USD    -> booked to the 100.00 lot
  Assets:Cash    750.00 USD
  Income:Gain                               -> interpolated to -250.00 USD
```

The weight of the stock leg is −500.00 USD (5 × cost 100.00). Proceeds are
+750.00. The 250.00 difference *must* be posted somewhere, and that somewhere
is a gains account. Realized gain is therefore a real posting to
`Income:Gains:Realized`, created by the ledger during booking when a reduction's
price differs from its cost.

Consequences:
- The `record()` path must be able to *generate* the gain posting, not merely
  validate it. Interpolation (§6) is the mechanism: the caller leaves the gains
  posting open and the ledger fills it.
- Gains accounts must exist before the first sale. The library should create
  them on demand under a configurable root.
- Unrealized gain remains a query — computed from the price table against
  held lots (§9), never posted.

---

## 8. Assertions and padding

### Balance assertions

`(date, account, amount, commodity)` — assert that the account's balance in
that commodity equals the amount at the start of that date.

Three semantics verified against Beancount 3.2.3 that must be decided
explicitly rather than assumed:

1. **Assertions include sub-accounts.** Asserting `Assets:Bank 0.00 USD` fails
   when `Assets:Bank:Sub` holds 100.00; asserting 100.00 passes. Adopt this —
   it is what makes an assertion against a Plaid-reported institution balance
   meaningful when the product splits an account into sub-accounts.
2. **Assertions are per-commodity.** An assertion in USD says nothing about
   other commodities in the same account.
3. **Assertion tolerance derives from the precision of the asserted number**,
   not the account's holdings. Asserting `100.004` against a true `100.00`
   fails, because three decimals implies a 0.0005 tolerance.

Assertions are stored as rows with `status` and `difference`, so a failure is
data the product can render, not an exception that halts a sync.

### Pad

A `pad` directive inserts an automatic balancing transaction so that the next
assertion on that account succeeds, booking the difference to a named equity
account. Verified: `pad Assets:Bank Equity:Opening-Balances` followed by
`balance Assets:Bank 500.00 USD` generates a 500.00 opening transaction.

**This matters more for Obol than it does for Beancount users.** Plaid connects
an account that already has a balance and typically supplies only ~24 months of
history. Pad is exactly the mechanism for "this account existed before Obol
did": open it, pad it against the first Plaid-reported balance, and every
subsequent number is correct without fabricating history.

Padding transactions must be flagged as machine-generated and be
distinguishable in every report.

---

## 9. Prices and valuation

Price data is high-volume: the 2.5-year example ledger holds **822 price
directives against 1,028 transactions**. Treat the price table as a first-class,
indexed, frequently-queried structure rather than an afterthought.

- `(date, commodity, quote_commodity, price)`, unique on the triple
- Lookup is "most recent price at or before date," which needs a covering index
  on `(commodity, quote_commodity, date DESC)`
- Market value of a holding = quantity × price at date, in the operating
  currency
- Unrealized gain = market value − cost basis of held lots
- An `operating_currency` ledger option defines the reporting currency

Prices are also implicitly available from transactions that carry `@ price`.
Beancount can synthesize price points from these; Obol should record them
explicitly at write time into the same table, tagged with their origin.

---

## 10. Schema

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE commodities (
    id                INTEGER PRIMARY KEY,
    symbol            TEXT NOT NULL UNIQUE,
    name              TEXT,
    kind              TEXT NOT NULL
                      CHECK (kind IN ('currency','security','tracking')),
    display_precision INTEGER NOT NULL DEFAULT 2,
    first_date        TEXT,
    metadata          TEXT
);

CREATE TABLE accounts (
    id                  INTEGER PRIMARY KEY,
    path                TEXT NOT NULL UNIQUE,
    type                TEXT NOT NULL CHECK (type IN
                          ('ASSET','LIABILITY','EQUITY','INCOME','EXPENSE')),
    parent_id           INTEGER REFERENCES accounts(id),
    opened_on           TEXT NOT NULL,
    closed_on           TEXT,
    booking_method      TEXT NOT NULL DEFAULT 'STRICT'
                        CHECK (booking_method IN
                          ('STRICT','FIFO','LIFO','SPECIFIC','NONE')),
    allowed_commodities TEXT,            -- JSON array, NULL = any
    metadata            TEXT,
    CHECK (closed_on IS NULL OR closed_on >= opened_on)
);
CREATE INDEX idx_accounts_path ON accounts(path);

CREATE TABLE transactions (
    id            INTEGER PRIMARY KEY,
    date          TEXT NOT NULL,               -- ISO-8601
    flag          TEXT NOT NULL DEFAULT '*',
    payee         TEXT,
    narration     TEXT,
    source        TEXT,                        -- 'plaid'|'manual'|'pad'|...
    source_ref    TEXT,
    reverses_id   INTEGER REFERENCES transactions(id),
    generated     INTEGER NOT NULL DEFAULT 0,  -- pad/gain machinery
    created_at    TEXT NOT NULL,
    metadata      TEXT
);
CREATE INDEX idx_txn_date ON transactions(date);
CREATE UNIQUE INDEX idx_txn_source ON transactions(source, source_ref)
    WHERE source_ref IS NOT NULL;

CREATE TABLE postings (
    id               INTEGER PRIMARY KEY,
    transaction_id   INTEGER NOT NULL REFERENCES transactions(id),
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    seq              INTEGER NOT NULL,         -- order within transaction

    units            INTEGER NOT NULL,         -- scale 8
    units_precision  INTEGER NOT NULL,         -- decimals as written
    commodity_id     INTEGER NOT NULL REFERENCES commodities(id),

    cost_per_unit    INTEGER,                  -- scale 8
    cost_commodity   INTEGER REFERENCES commodities(id),
    cost_date        TEXT,
    cost_label       TEXT,

    price_per_unit   INTEGER,                  -- scale 8
    price_commodity  INTEGER REFERENCES commodities(id),

    weight           INTEGER NOT NULL,         -- scale 8, denormalized
    weight_commodity INTEGER NOT NULL REFERENCES commodities(id),

    flag             TEXT,
    interpolated     INTEGER NOT NULL DEFAULT 0,
    metadata         TEXT,
    UNIQUE (transaction_id, seq)
);
CREATE INDEX idx_post_account_txn ON postings(account_id, transaction_id);
CREATE INDEX idx_post_txn ON postings(transaction_id);

CREATE TABLE lots (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    commodity_id      INTEGER NOT NULL REFERENCES commodities(id),
    acquired_on       TEXT NOT NULL,
    original_quantity INTEGER NOT NULL,        -- scale 8
    cost_per_unit     INTEGER NOT NULL,        -- scale 8
    cost_commodity    INTEGER NOT NULL REFERENCES commodities(id),
    label             TEXT,
    opened_by_posting INTEGER NOT NULL REFERENCES postings(id)
);
CREATE INDEX idx_lots_lookup
    ON lots(account_id, commodity_id, acquired_on);

CREATE TABLE lot_reductions (
    id         INTEGER PRIMARY KEY,
    lot_id     INTEGER NOT NULL REFERENCES lots(id),
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    quantity   INTEGER NOT NULL                -- scale 8, positive
);
CREATE INDEX idx_lotred_lot ON lot_reductions(lot_id);

CREATE TABLE tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE transaction_tags (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    tag_id         INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (transaction_id, tag_id)
);

CREATE TABLE links (                      -- ties related transactions together
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    name           TEXT NOT NULL,
    PRIMARY KEY (transaction_id, name)
);

CREATE TABLE balance_assertions (
    id           INTEGER PRIMARY KEY,
    date         TEXT NOT NULL,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    amount       INTEGER NOT NULL,             -- scale 8
    precision    INTEGER NOT NULL,             -- drives tolerance
    commodity_id INTEGER NOT NULL REFERENCES commodities(id),
    source       TEXT,
    checked_at   TEXT,
    status       TEXT CHECK (status IN ('pass','fail','unchecked')),
    difference   INTEGER
);
CREATE INDEX idx_assert_account_date
    ON balance_assertions(account_id, date);

CREATE TABLE pads (
    id             INTEGER PRIMARY KEY,
    date           TEXT NOT NULL,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    source_account INTEGER NOT NULL REFERENCES accounts(id),
    generated_txn  INTEGER REFERENCES transactions(id)
);

CREATE TABLE prices (
    id              INTEGER PRIMARY KEY,
    date            TEXT NOT NULL,
    commodity_id    INTEGER NOT NULL REFERENCES commodities(id),
    price           INTEGER NOT NULL,          -- scale 8
    quote_commodity INTEGER NOT NULL REFERENCES commodities(id),
    origin          TEXT,                      -- 'directive'|'transaction'|'fetch'
    UNIQUE (date, commodity_id, quote_commodity)
);
CREATE INDEX idx_prices_lookup
    ON prices(commodity_id, quote_commodity, date DESC);

CREATE TABLE notes (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL,
    account_id INTEGER REFERENCES accounts(id),
    comment    TEXT NOT NULL
);

CREATE TABLE documents (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL,
    account_id INTEGER REFERENCES accounts(id),
    path       TEXT NOT NULL,
    sha256     TEXT
);

CREATE TABLE events (
    id    INTEGER PRIMARY KEY,
    date  TEXT NOT NULL,
    type  TEXT NOT NULL,          -- 'employer','address',...
    value TEXT NOT NULL
);

CREATE TABLE balance_checkpoints (     -- pure cache, always rebuildable
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    commodity_id INTEGER NOT NULL REFERENCES commodities(id),
    date         TEXT NOT NULL,
    balance      INTEGER NOT NULL,
    PRIMARY KEY (account_id, commodity_id, date)
);

CREATE TABLE ledger_options (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);   -- operating_currency, default booking_method, tolerance multiplier,
     -- gains account root, opening-balances account

CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

Notes:

- `weight` denormalized onto postings makes the zero-sum check a single
  `GROUP BY transaction_id, weight_commodity HAVING SUM(weight) != 0` over the
  entire database.
- `seq` preserves posting order, which matters for faithful export and for
  reproducing a paycheck in its original shape.
- `idx_txn_source` gives free deduplication for Plaid ingestion.
- `notes`, `documents` and `events` are cheap and directly serve the
  "financial hub" goal — attaching a statement PDF or a policy document to an
  account is exactly the kind of thing a hub should do and a budgeting app
  never does.
- No `UPDATE` path exists in the repository layer for `postings` or
  `transactions`. Enforce with `BEFORE UPDATE` triggers once §15/M4 lands.

---

## 11. Immutability and corrections

Two ways a recorded transaction turns out wrong:

**Source revision** — Plaid changes a pending amount or removes a transaction.
The product detects it via `source_ref` and calls `replace()`.

**User correction** — wrong category, discovered later.

Both are implemented as a reversal (same postings negated, dated on the date of
discovery, not backdated) plus a new transaction, linked via `reverses_id`. The
original stays queryable forever.

Reversing a transaction that reduced a lot must also reverse the
`lot_reductions` rows, restoring the lot's remaining quantity. Reversing an
acquisition must fail if the lot has since been reduced — the correct action
there is to reverse the dependent reductions first. This ordering constraint is
a validator check, not a convention.

**Note on scope:** categorization *before* commit is free. A transaction that
has not entered the ledger has nothing to reverse. The review queue lives
upstream of the ledger, so ordinary re-categorization never generates
reversals. Reversals are for what was already committed.

---

## 12. Validation

A validator that runs over the whole database and returns a structured report.
This is what makes a homegrown ledger trustworthy.

1. Every transaction's weights sum to zero per commodity, within tolerance
2. Every transaction has at least two postings
3. At most one interpolated posting per commodity per transaction
4. No posting references an account outside its open/close window
5. No posting violates its account's allowed-commodity constraint
6. Every balance assertion matches the computed balance on its date, at the
   assertion's own tolerance, including sub-accounts
7. Lot reductions never exceed the lot's original quantity
8. No lot has negative remaining quantity at any point in time
9. Reductions are booked only against lots in the same account and commodity
10. **Global integrity: the sum of all weights across the entire ledger is
    zero, per commodity.** If this passes, no money has been invented or
    destroyed anywhere in the system.
11. Every posting belongs to a transaction; no orphans
12. Account hierarchy is acyclic; every parent exists
13. Every generated (pad / gain) transaction traces to its originating
    directive
14. Checkpoints, if present, match naive recomputation

Run after every ingestion batch, before every backup, and on demand from a CLI.
Sub-second on a personal ledger.

---

## 13. Public API

```python
class Ledger:
    # construction
    @classmethod
    def open(cls, path) -> "Ledger"          # owns its connection
    def __init__(self, connection) -> None   # embedded: caller owns it

    # setup
    def create_commodity(symbol, kind, display_precision=2, **meta) -> Commodity
    def create_account(path, type, opened_on, **opts) -> Account
    def close_account(path, closed_on) -> None
    def set_option(key, value) -> None

    # writing
    def record(spec: TransactionSpec) -> Transaction
    def reverse(transaction_id, on_date, reason) -> Transaction
    def replace(transaction_id, new: TransactionSpec) -> Transaction
    def assert_balance(account, date, amount, commodity, source) -> Assertion
    def pad(account, source_account, date) -> Pad
    def record_price(commodity, date, price, quote_commodity, origin) -> None

    # reading
    def balance(account, on, *, include_children=True) -> Inventory
    def inventory(account, on) -> Inventory              # lot-level
    def journal(account, start, end) -> list[Posting]
    def balance_sheet(on) -> Statement
    def income_statement(start, end) -> Statement
    def market_value(account, on, in_commodity) -> Amount
    def unrealized_gain(account, on, in_commodity) -> Amount

    # integrity & interop
    def validate() -> ValidationReport
    def export_beancount(path) -> None
    def import_beancount(path) -> ImportReport          # corpus testing
```

`record()` computes weights, interpolates missing amounts, resolves booking,
generates any required gains posting, validates, and writes — all inside one
SQLite transaction. Either the whole balanced transaction lands or none of it
does.

`balance()` returns an `Inventory`, not a number. An account may hold multiple
commodities. For single-currency accounts it collapses to one amount, but the
type is honest about the general case.

**Two constructors.** `Ledger.open(path)` owns its connection and is what tests,
the CLI, and corpus runs use. `Ledger(connection)` borrows a connection the
caller owns, so an embedding application can wrap a ledger write and its own
write in a single SQLite transaction. The library touches only its own tables
either way. Build both in M1 — retrofitting connection injection later means
touching every repository.

---

## 14. Testing strategy

This is where reliability is actually won. Four layers, in increasing power.

### Layer 1 — pure domain unit tests

Balancing, tolerance inference, interpolation, inventory arithmetic, and
booking are pure functions over plain objects. Test exhaustively with no
database.

### Layer 2 — property-based tests (Hypothesis)

Generate random valid transactions and assert invariants:

- Anything `record()` accepts has weights summing to zero per commodity
- Balance after N transactions equals the sum of their postings
- Reversing a transaction returns every affected balance to its prior value
- FIFO and LIFO consume identical total quantities, differing only in cost
- Checkpointed balances always equal naively-computed balances
- Export → import → export is byte-identical (round-trip stability)

### Layer 3 — the bean-example corpus

`bean-example` generates a realistic multi-year ledger: paychecks with
withholding and 401k, brokerage buys and sells with cost basis, dividends,
vacation accrual, price history, and periodic balance assertions. Generate
several with different seeds and date ranges:

```
bean-example --seed 1 --date-begin 2015-01-01 --date-end 2026-01-01 -o corpus1.beancount
```

For each corpus file:

1. `import_beancount()` it into a fresh SQLite ledger
2. Assert zero import errors
3. Load the same file with `beancount.loader.load_file`
4. **Compare, per account and per commodity, the final balance and the
   full lot-level inventory**
5. Compare realized gains per gains account
6. Compare market value at several dates against Beancount's price lookup
7. Run Obol's own `validate()` and assert a clean report
8. Export back to `.beancount`, reload with Beancount, and assert the balances
   still match

Pass bar: **exact equality on every account, every commodity, every date
tested.** Not approximate. A discrepancy of one cent is a bug.

This layer is the reason the importer exists. It is not a product feature — it
is test infrastructure that buys a 2,000-directive, semantically rich,
independently-verified fixture for the cost of writing a parser adapter.

### Layer 4 — differential fuzzing

Generate random ledgers with Hypothesis, export to Beancount text, load both,
compare balances. This finds the cases `bean-example` does not generate.

Guard against a false positive: **a buggy exporter can manufacture agreement.**
If exporter and ledger share a misunderstanding, Beancount agrees with both.
Mitigate by keeping the exporter a dumb serializer with no logic, and testing
it separately against hand-written expected output.

### Golden scenarios

Hand-written, hand-verified, kept as documentation:

- A month of ordinary spending across two accounts
- A credit-card cycle: purchases, a partial payment, a refund
- A transfer between own accounts
- An 18-posting paycheck with three commodities
- A stock purchase, a partial FIFO sale, and the resulting realized gain
- An account connected mid-life: open, pad, first assertion
- A Plaid pending→posted amount revision, via `replace()`

### Pinning

Pin an exact Beancount version as a dev dependency. It is the oracle; an
upstream change must not silently move it. Note that Beancount 3 split ingest,
query, and price fetching into `beangulp`, `beanquery`, and `beanprice` — only
the core is needed here.

### Licensing note

Beancount is GPL-2.0. Running it as a dev-time test dependency and generating
corpus files with its tools is unambiguously fine for a private project.
Copying its test files or source into the repository is a different question.
Transcribing *scenarios* into tests written from scratch is fine. This is worth
a proper look only if Obol is ever distributed.

---

## 15. Milestones

Each independently useful and independently testable.

**M1 — Skeleton, single currency.**
Commodities, accounts, transactions, postings, weights, tolerance inference,
interpolation. `record()`, `balance()`, `journal()`. Layers 1 and 2 testing.
*Done when: a hand-entered month of expenses produces correct balances and a
random-transaction property test passes.*

**M2 — Beancount export.**
Dumb serializer, round-trip tests. Pulled forward from revision 1's M6.
*Done when: `bean-check` passes on exported output and Beancount's balances
match Obol's on M1 data.*

Everything after this point is built with an oracle attached.

**M3 — Hierarchy and statements.**
Account tree, rollups, balance sheet, income statement, date ranges.
*Done when: net worth and a category breakdown are one call each.*

**M4 — Assertions, padding, validation.**
Balance assertions with sub-account accumulation and precision-derived
tolerance, pad directives, the full validator, a CLI.
*Done when: an injected corrupt row is caught, and a mid-life account
connection reconciles correctly.*

**M5 — Corrections.**
Reverse and replace, `source_ref` dedup, lot-reduction reversal ordering.
*Done when: replacing a transaction leaves balances correct and history
intact.*

**M6 — Tags, links, metadata, notes, documents, events.**
The second categorization axis and the hub-oriented attachments.
*Done when: a query slices by tag across account boundaries.*

**M7 — Multi-commodity, cost, lots, booking, gains.**
Weights with cost and price, lot creation and reduction, STRICT/FIFO/LIFO/
SPECIFIC, generated realized-gain postings, price table, market valuation,
unrealized gain.
*Done when: buying and partially selling a stock produces correct cost basis
and realized gain matching Beancount exactly.*

**M8 — The corpus gate.**
Beancount importer, the full Layer 3 comparison, differential fuzzing.
*Done when: several multi-year `bean-example` ledgers import, validate, and
match Beancount exactly on every account, commodity, and tested date.*

M1–M6 cover everything product phase 1 needs. M7 is product phase 2's
prerequisite and can be built at any point without disturbing M1–M6 — which is
the entire point of designing the commodity model in from the start. M8 is what
lets you trust the whole thing with real money.

---

## 16. Deliberate divergences from Beancount

Written down so differential tests can account for them.

| Area | Beancount | Obol | Why |
|---|---|---|---|
| Storage | Plain text files | SQLite | Concurrent writes, webhook ingestion, query patterns |
| Booking resolution | Recomputed on every parse | Resolved once, stored | Determinism; a policy change must not rewrite history |
| Numbers | `Decimal` throughout | int64 at scale 8 + recorded precision | Native SQL aggregation |
| Corrections | Edit the text file | Append-only reversals | Auditability under automated ingestion |
| `AVERAGE` booking | Present but unimplemented | Not supported | No oracle to test against |
| Plugins | First-class extension system | Not supported | Unnecessary for a single-user ledger |
| Directive ordering | File order significant for some ops | Explicit dates and `seq` | No file to order |

Everything not in this table is intended to match Beancount exactly, and the
corpus tests are what prove it.

---

## 17. Corrections to revision 1

Recorded so the reasoning is not lost.

1. **Realized gain is a posting, not a query.** Revision 1 said it was computed
   at query time. It cannot be — the transaction would not balance. §7.
2. **Per-commodity integer scaling was wrong.** A commodity's quantity
   precision and its price precision differ. Replaced with a global scale of 8
   plus per-amount recorded precision. §5.
3. **Tolerance rule was hand-waved.** Replaced with Beancount's actual
   inference from written decimal precision. §6.
4. **Default booking should be STRICT, not FIFO.** Matches Beancount, and an
   ambiguous reduction from an importer is a signal, not something to
   silently resolve. §7.
5. **Interpolation was missing entirely.** It is both a Beancount feature and
   the natural shape of the Plaid pipeline. §6.
6. **Pad was missing.** It is the mechanism for connecting an account that
   already has a balance and no full history — a core Obol scenario. §8.
7. **Balance assertion semantics were unspecified.** Sub-account accumulation
   and precision-derived tolerance both verified and adopted. §8.
8. **Non-monetary tracking commodities were missing.** They are how 401k
   contribution limits and PTO get tracked in the same ledger as money. §4.
9. **Prices were underweighted.** They outnumber transactions in realistic
   data and need first-class indexing. §9.
10. **Export moved earlier**, from M6 to M2, so every subsequent milestone is
    built with an oracle attached. §15.
11. **Notes, documents, events added** — cheap, and they serve the hub goal.

---

## 18. Decisions still open

- **Gains account granularity** — one `Income:Gains:Realized`, or per-account
  (`Income:Gains:ETrade`)? Per-account gives better attribution; one account is
  simpler. Leaning per-institution.
- **Transaction-level vs posting-level dates.** Some systems allow a posting to
  settle on its own date. Recommend transaction-level only until a real need
  appears.
- **Append-only enforcement**: triggers (stronger) or repository discipline
  (simpler). Recommend triggers once M5 lands.
- **Currency constraints on accounts** — the example ledger constrains 45 of 63.
  Recommend constraining bank and card accounts, leaving investment accounts
  open.
- **How much of the operating-currency machinery to build in M1** versus
  deferring to M7 with the rest of valuation.
