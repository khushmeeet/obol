# Obol Ledger — Implementation Plan

Companion to `obol-ledger-design.md`. That document is the *what*; this is the
*how, in what order, and how you know each step is done*.

---

## 1. Working approach

Four rules that shape everything below.

**Build the oracle early, then never build blind.** M1 and M2 exist to get a
working Beancount comparison harness in place. Every milestone after that is
developed against an independent implementation that already knows the right
answer. This inverts the usual risk profile: instead of discovering semantic
errors in month four, you discover them the hour you write them.

**Two backends, one test suite.** Define a `Repository` protocol and implement
it twice — in-memory and SQLite. Run the entire test suite against both,
parametrized. The in-memory backend keeps domain tests fast and forces the
domain layer to stay genuinely free of SQL. Disagreement between the two
backends is a bug in the SQLite layer, caught immediately and for free.

**Invariants are executable, not documentation.** Every rule in the design doc
that says "must" becomes a check in `validation/checks.py` and a property test.
If it isn't executable, it isn't enforced.

**Vertical slices, not horizontal layers.** Do not build the whole domain
layer, then the whole storage layer, then the whole API. Build the thinnest
path from `Ledger.record()` to a correct `Ledger.balance()`, then widen it.
A working narrow system beats three finished layers that have never been
connected.

---

## 2. Project setup

### Tooling

| Concern | Choice | Why |
|---|---|---|
| Python | 3.12+ | Modern typing, `Decimal` performance |
| Dependency management | `uv` | Fast, lockfile, handles dev/prod split cleanly |
| Testing | `pytest` | Parametrization is central to the two-backend approach |
| Property testing | `hypothesis` | Load-bearing here, not optional |
| Linting / formatting | `ruff` | Single tool, fast |
| Type checking | `mypy --strict` on `domain/` | The domain layer is pure; strict typing costs little and catches sign/unit errors |
| Migrations | Hand-rolled SQL files + version table | Alembic is overkill for one SQLite file |
| Oracle | `beancount==3.2.3` (dev only, pinned exact) | Test dependency, never a runtime one |

Note that `beancount` belongs in dev dependencies only. If it ever appears in
the runtime dependency set, something has gone wrong architecturally.

### Repository layout

The library is its own installable package with its own test suite, so it can
be developed, tested, and versioned without any product code present.

```
ledger/
  pyproject.toml
  README.md
  DESIGN.md                    <- the design doc
  PLAN.md                      <- this document
  src/ledger/                  <- src layout, so tests import the installed pkg
    domain/
    storage/
      migrations/
        0001_initial.sql
    validation/
    query/
    interop/
    cli.py
    api.py
  tests/
    unit/                      <- pure domain
    properties/                <- hypothesis
    golden/                    <- hand-verified scenarios
      scenarios/*.yaml
    corpus/                    <- bean-example comparison
      fixtures/*.beancount
    conftest.py
  tools/
    generate_corpus.sh
```

Use the `src/` layout. It prevents tests from accidentally importing the
working directory instead of the installed package — a real source of
"passes locally, fails in CI."

Whether this lives in its own repository or as a workspace package inside a
larger one is a product-side decision and does not affect anything here. What
matters for the library is that it installs and tests standalone, with neither
a web framework nor a bank API present. That constraint is what keeps the
boundary in design §1 mechanical rather than aspirational — and it is what
keeps the corpus gate honest.

### CI from commit one

GitHub Actions running: `ruff check`, `ruff format --check`,
`mypy --strict src/ledger/domain`, `pytest`. Add the corpus job as a
separate, slower workflow once M8 exists, plus the nightly fuzzing job.

Set up CI before writing M1. It is ten minutes of work at the start and an
afternoon of retrofitting later.

### Glue

A `justfile` with `just test`, `just migrate`, `just backup`, `just corpus`.
Backups are `VACUUM INTO` a timestamped file, run before every migration.

---

## 3. Milestone M1 — Walking skeleton, single currency

**Goal:** record a two-posting USD transaction and read back a correct balance,
through both backends, with tolerance and interpolation working.

Build in this order. Each step is test-first: write the failing test, then the
code.

### 1.1 `domain/amount.py`

```python
@dataclass(frozen=True)
class Commodity:
    symbol: str
    kind: CommodityKind          # CURRENCY | SECURITY | TRACKING
    display_precision: int = 2

@dataclass(frozen=True)
class Amount:
    value: int                   # scale 8
    precision: int               # decimals as written
    commodity: Commodity

    @classmethod
    def from_decimal(cls, d: Decimal, commodity: Commodity) -> "Amount": ...
    def to_decimal(self) -> Decimal: ...
    def __add__, __sub__, __neg__      # same-commodity only, else raise
    def multiply(self, factor: Decimal) -> "Amount":  # scale 16 internally, ROUND_HALF_EVEN to 8
```

The `precision` field is derived from the `Decimal`'s exponent on construction.
`Decimal("50.00")` gives precision 2; `Decimal("50.000")` gives 3. This is the
whole basis of tolerance inference, so get it right here and it is right
everywhere.

Tests: round-trip `Decimal -> Amount -> Decimal` for a wide range including
negatives, zeros, and 8-decimal values; rejection of cross-commodity
arithmetic; rounding behaviour at the half.

### 1.2 `domain/accounts.py`

`AccountType` enum, path validation (segments, allowed characters, capitalized
root matching the type), `parent_path()`, `is_descendant_of()`.

Tests: path parsing edge cases; type/root agreement; rejection of malformed
paths.

### 1.3 `domain/transaction.py`

The important distinction:

```python
@dataclass
class PostingSpec:            # input — may be incomplete
    account: str
    units: Amount | None      # None = interpolate me
    cost: CostSpec | None = None
    price: Amount | None = None
    flag: str | None = None
    metadata: dict = field(default_factory=dict)

@dataclass
class TransactionSpec:        # input
    date: date
    postings: list[PostingSpec]
    payee: str | None = None
    narration: str | None = None
    ...

@dataclass(frozen=True)
class Posting:                # committed — fully resolved
    ...
    units: Amount             # never None
    weight: Amount
    interpolated: bool
```

Keeping input and committed types distinct is what stops half-resolved state
leaking into storage. `record()` is the only function that turns one into the
other.

### 1.4 `domain/balancing.py`

Three pure functions, the heart of the system:

```python
def compute_weight(posting: PostingSpec) -> Amount | None
def infer_tolerances(spec: TransactionSpec) -> dict[Commodity, Decimal]
def balance_transaction(spec: TransactionSpec) -> list[Posting]   # raises on failure
```

`balance_transaction` groups by commodity, computes weights, finds the residual
per commodity, fills at most one interpolated posting per commodity, and then
asserts every residual is within tolerance. This function will be the most
heavily tested code in the project — budget for that.

Tests: two-posting balance; the 18-posting three-commodity paycheck; a
transaction balancing in USD but not in a tracking commodity (must fail);
interpolation filling one leg; two missing legs in one commodity (must fail);
residual just inside and just outside tolerance.

### 1.5 `storage/`

`schema.sql` from design §10 (only the tables M1 needs — commodities,
accounts, transactions, postings, ledger_options, schema_migrations; add the
rest as their milestones land, each as a new migration).

`db.py`: connection factory setting `foreign_keys=ON` and `journal_mode=WAL`,
plus a `unit_of_work()` context manager so a whole transaction commits or
rolls back atomically.

`repositories.py`: the `Repository` protocol and its SQLite implementation.
Then a parallel `InMemoryRepository`. `conftest.py` parametrizes every
integration test over both.

### 1.6 `api.py`

`Ledger.create_commodity`, `create_account`, `record`, `balance`, `journal`.

`record()` sequence, which never changes across later milestones — later work
inserts steps, it does not reorder them:

```
validate accounts exist and are open on the date
compute weights
infer tolerances
interpolate missing postings
[M7: resolve booking, generate gains posting]
assert balanced per commodity
open unit of work
  insert transaction
  insert postings
commit
```

### 1.7 Testing

Unit tests on everything above. Two property tests to start:

- Anything `record()` accepts has per-commodity weights summing to zero
- Balance after N recorded transactions equals the summed postings for that
  account

**Exit criteria.** A hand-entered month of ordinary expenses across two
accounts produces correct balances on both backends. The paycheck golden
scenario records and balances. `mypy --strict` clean on `domain/`.

*Rough size: the largest milestone. Expect several sessions — `balancing.py`
alone deserves one.*

---

## 4. Milestone M2 — Beancount export and the oracle harness

**Goal:** a working comparison against Beancount, in place before any further
semantics are built.

### 2.1 `interop/export.py`

A deliberately dumb serializer. No logic, no inference, no cleverness — it
prints what is in the database. If you feel the urge to compute something in
the exporter, that computation belongs in the ledger.

Emit: `option` lines, `commodity`, `open`/`close`, transactions with payee,
narration, tags, links, and postings with cost `{...}` and price `@ ...`.

Formatting must be precision-faithful: an amount stored with `precision=2`
prints as `50.00`, not `50` or `50.000000`. Beancount infers its tolerances
from what it reads, so a formatting error becomes a tolerance error and the
comparison silently changes meaning.

### 2.2 `tests/conftest.py` — the oracle fixture

```python
def assert_matches_beancount(ledger, *, dates=None):
    text = ledger.export_beancount_string()
    entries, errors, options = beancount.loader.load_string(text)
    assert not errors, [str(e) for e in errors]
    # compare per account, per commodity balances (and later, lot inventories)
```

Compare **balances and inventories, not directive structures.** Comparing your
objects against Beancount's NamedTuples means writing a translation layer that
itself needs testing, and it couples your tests to their internal shape.

### 2.3 Exporter tests

Hand-written expected output for each golden scenario, byte-compared. This is
the guard against the false-positive failure mode where a buggy exporter and a
buggy ledger agree with each other.

**Exit criteria.** `bean-check` passes on exported M1 data. Beancount's computed
balances match Obol's exactly on every golden scenario. The
`assert_matches_beancount` fixture is available to every later test.

*Rough size: one session. The payoff runs for the rest of the project.*

---

## 5. Milestone M3 — Hierarchy and statements

`query/balances.py` — balance with and without children, at a date.
`query/statements.py` — balance sheet (assets/liabilities/equity at a point),
income statement (income/expenses over a range), both driven purely off
`account.type`.

Sign flipping for display lives **here and only here**. The rest of the system
stays in raw signed amounts. One place to reason about it, one place to get it
wrong.

Rollup implementation: prefix match on `path`, with an index. Do not build a
recursive CTE until a query is measurably slow.

**Exit criteria.** Net worth is one call. A category breakdown with drill-down
is one call. Both match hand-computed values on the golden scenarios.

*Rough size: one session.*

---

## 6. Milestone M4 — Assertions, padding, validation

### 4.1 Balance assertions

Store, then check. A checker that computes the account balance (including
sub-accounts) at the assertion date and compares within the tolerance implied
by the assertion's own precision. Write `status` and `difference` back to the
row — a failure is data the product renders, not an exception that halts a
sync.

### 4.2 Pad

`pad(account, source_account, date)` records the directive; the generated
transaction is created when the next assertion on that account is evaluated,
with `generated=1` and `source='pad'`. Padding must be visible and filterable
in every report.

Test explicitly against Beancount, which has subtle rules about pad/assertion
interaction — this is a place where "roughly the same" is not good enough.

### 4.3 `validation/`

All fourteen checks from design §12, each a separate function returning
structured findings, plus a `validator.py` that runs them and returns a report.
`cli.py` gets `ledger validate`.

Write a test that deliberately corrupts a row — flips a sign, deletes one
posting of a pair, backdates a posting outside its account's lifetime — and
asserts the validator catches each one. A validator that has never caught
anything is not known to work.

**Exit criteria.** An injected corruption is caught. A mid-life account
connection (open → pad → first Plaid balance assertion) reconciles correctly
and matches Beancount.

*Rough size: two sessions. Pad semantics are fiddlier than they look.*

---

## 7. Milestone M5 — Corrections

`reverse()`, `replace()`, `source_ref` deduplication.

The subtle part is lot interaction, even though lots don't exist until M7:
design the reversal path now so that reversing a lot-reducing posting also
reverses its `lot_reductions`, and reversing a lot-creating posting fails if
that lot has since been reduced. Leave it as a stub with a raising branch and a
test marked `xfail` until M7 fills it in — the ordering constraint is easy to
overlook if it isn't written down as a failing test.

Append-only enforcement: add `BEFORE UPDATE` and `BEFORE DELETE` triggers on
`transactions` and `postings` that raise. Test that they fire.

**Exit criteria.** Replacing a transaction leaves balances correct and history
intact. Repeated ingestion of the same `source_ref` is a no-op. Direct
`UPDATE` against the tables is refused by the database.

*Rough size: one to two sessions.*

---

## 8. Milestone M6 — Tags, links, metadata, notes, documents, events

Mostly schema and plumbing. The one design decision worth care: tag queries
must work across the account hierarchy, so index for
`tag -> transactions -> postings -> accounts` traversal.

`notes`, `documents`, `events` are small tables with small APIs. Documents
store a path and a SHA-256; the library does not manage file storage, only
references.

**Exit criteria.** A query slices spending by tag across account boundaries and
matches a hand-computed answer.

*Rough size: one session.*

---

## 9. Milestone M7 — Commodities, cost, lots, booking, gains

The hardest milestone. Sequence it internally:

**7.1 Weights with cost and price.** Extend `compute_weight` for the cost and
price cases. Test the sale shape from design §4 — cost determines weight, price
does not.

**7.2 `domain/inventory.py`.** `Position` (units at a cost), `Inventory` (a
collection), addition, reduction, and querying by lot. Pure, heavily unit
tested.

**7.3 `domain/booking.py`.** Resolution of a reduction against available lots,
per method. Signature roughly:

```python
def book_reduction(
    inventory: Inventory,
    posting: PostingSpec,
    method: BookingMethod,
) -> list[LotMatch]         # raises AmbiguousReduction under STRICT
```

Port the *scenarios* from Beancount's `booking_full_test.py` and
`booking_method_test.py` as your own table of cases: starting inventory,
reduction, expected matches. You are borrowing their enumeration of the hard
cases, which is the valuable part; the assertions are yours. (Transcribe, don't
copy files — see design §14 on licensing.)

**7.4 Lot persistence.** `lots` and `lot_reductions` tables; acquisition
creates, reduction records matches. Wire in the M5 reversal path and remove the
`xfail`.

**7.5 Generated gains postings.** When a reduction's price differs from its
cost, the residual is posted to the gains account. Implemented through the
existing interpolation mechanism — the caller leaves the gains leg open, the
ledger fills it. Auto-create the gains account under the configured root if it
does not exist.

**7.6 Prices and valuation.** The `prices` table, "most recent at or before
date" lookup, market value, unrealized gain. Record `@ price` observations from
transactions into the same table with `origin='transaction'`.

**Exit criteria.** Buy, partially sell under FIFO, and produce cost basis and
realized gain matching Beancount exactly. STRICT correctly rejects an ambiguous
reduction. Market value at several dates matches Beancount's own price lookup.

*Rough size: the second largest milestone. Three or four sessions.*

---

## 10. Milestone M8 — The corpus gate

### 8.1 `interop/import_.py`

A Beancount reader. Use `beancount.loader` to parse (no reason to write a
parser), then map its directives onto `Ledger` calls. This is test
infrastructure, so depending on Beancount here is fine — but keep it in
`interop/` and out of the runtime path.

Map: `Open`/`Close` → accounts, `Commodity` → commodities, `Transaction` →
`record()`, `Balance` → `assert_balance()`, `Pad` → `pad()`, `Price` →
`record_price()`, `Note`/`Document`/`Event` → their tables.

### 8.2 `tools/generate_corpus.sh`

```bash
for seed in 1 2 3 4 5; do
  bean-example --seed "$seed" \
    --date-begin 2015-01-01 --date-end 2026-01-01 \
    -o "tests/corpus/fixtures/corpus_${seed}.beancount"
done
```

Commit the fixtures rather than generating at test time — a generator change
upstream should not silently alter your test corpus.

### 8.3 The comparison test

For each corpus file, the eight-step sequence in design §14: import, assert no
errors, load with Beancount, compare final balances and lot-level inventories
per account and commodity, compare realized gains, compare market value at
sampled dates, run `validate()`, then export and re-verify.

**Pass bar: exact equality.** Not approximate. A one-cent discrepancy is a bug,
and chasing it will teach you something about your booking or rounding that you
need to know.

### 8.4 Differential fuzzing

Hypothesis generates random ledgers within the semantics you support; export,
load both, compare. This finds what `bean-example` does not generate. Run it as
a nightly CI job with a larger example budget rather than on every commit.

**Exit criteria.** Five multi-year corpora import, validate clean, and match
Beancount exactly. Nightly fuzzing runs green for a week.

*Rough size: two sessions, plus however long the first real discrepancy takes.
Budget for that discrepancy — there will be one, and it is the whole point.*

---

## 11. Sequencing and pacing

```
M1 skeleton ──> M2 export/oracle ──> M3 statements ──> M4 assertions+pad
                                                            │
                                          M5 corrections ◄──┘
                                                │
                            M6 tags/metadata ◄──┘
                                                │
                              M7 lots/gains ◄───┘
                                                │
                              M8 corpus gate ◄──┘
```

M1–M6 are everything product phase 1 needs. **Start building the Obol product
against the library after M6** — do not wait for M7 and M8. Phase 1 of the
product does not touch a single lot, and the library's public API does not
change when M7 lands. Building the product early also surfaces API ergonomics
problems while they are still cheap to fix.

M7 and M8 can then proceed in parallel with product work, and must both be
complete before the first real investment account is connected.

One caveat on the estimates above: they assume focused sessions and no
production pressure. The two that reliably overrun are `balancing.py` in M1 and
booking in M7. Both are worth the time.

---

## 12. Risks and how each is handled

| Risk | Handling |
|---|---|
| Subtle booking bug corrupts cost basis silently | M8 corpus gate; exact-equality pass bar |
| Exporter bug creates false agreement with the oracle | Exporter is a dumb serializer, tested independently against hand-written output |
| Sign errors in reports | Sign flipping confined to `query/statements.py`; golden scenarios assert display values |
| Rounding drift over years of data | Integer arithmetic at fixed scale 8; half-even rounding; global weight-sum check in `validate()` |
| Schema change needed after real data exists | Versioned migrations from commit one; migration tests against populated databases |
| Beancount upstream change moves the oracle | Exact pinned version; upgrade deliberately, as its own commit, with the corpus gate as the test |
| Library accretes product concerns | The §1 boundary test, enforced at review: would an accountant recognise it as accounting? |
| Losing data to a bug or a bad migration | `VACUUM INTO` backup before every migration; the Beancount export doubles as a plain-text archive |

---

## 13. Definition of done for the library

The library is finished — as distinct from the product — when:

1. Five multi-year `bean-example` corpora import, validate clean, and match
   Beancount exactly on every account, commodity, and sampled date
2. Nightly differential fuzzing has run green for a week
3. `mypy --strict` is clean on `domain/`
4. Every golden scenario is covered, including the three that matter most:
   the 18-posting multi-commodity paycheck, the mid-life account connection via
   pad, and the partial FIFO sale with realized gain
5. The validator catches every deliberately injected corruption in the test suite
6. `ledger` imports nothing product-specific, and `beancount` appears only
   in dev dependencies

At that point the accounting foundation is done and will not need to be
revisited when the product changes shape — which is the entire reason for
building it separately.
