-- M1 schema (design §10): commodities, accounts, transactions, postings,
-- ledger_options. Later milestones add their tables in their own
-- migrations. The postings table carries the full designed column set
-- (cost/price/weight) so M7 needs no schema change; cost and price stay
-- NULL until then.
--
-- schema_migrations is bootstrapped by db.migrate() before any migration
-- runs. PRAGMAs (foreign_keys, WAL) are connection-level and set in
-- db.connect().

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
-- The UNIQUE constraint on path already provides the lookup index.

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

    cost_per_unit    INTEGER,                  -- scale 8 (M7)
    cost_commodity   INTEGER REFERENCES commodities(id),
    cost_date        TEXT,
    cost_label       TEXT,

    price_per_unit   INTEGER,                  -- scale 8 (M7)
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

CREATE TABLE ledger_options (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
