-- M4: balance assertions and pad directives (design §8, §10).
--
-- balance_assertions rows carry their check outcome (status, difference,
-- checked_at) — a failed assertion is data the product renders, not an
-- exception that halts a sync.
--
-- pads adds one column over design §10: consumed_by. A pad is spent by
-- the first assertion evaluated on its account after it, whether or not
-- padding was needed (matching Beancount, where a pad whose next balance
-- check needs nothing becomes an "unused pad" rather than staying armed
-- and retroactively breaking that check later). generated_txn alone
-- cannot represent "spent but generated nothing".

CREATE TABLE balance_assertions (
    id           INTEGER PRIMARY KEY,
    date         TEXT NOT NULL,               -- asserted at the START of this date
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    amount       INTEGER NOT NULL,            -- scale 8
    precision    INTEGER NOT NULL,            -- decimals as written; drives tolerance
    commodity_id INTEGER NOT NULL REFERENCES commodities(id),
    source       TEXT,
    checked_at   TEXT,
    status       TEXT NOT NULL DEFAULT 'unchecked'
                 CHECK (status IN ('pass','fail','unchecked')),
    difference   INTEGER                      -- scale 8, computed - asserted
);
CREATE INDEX idx_assert_account_date ON balance_assertions(account_id, date);

CREATE TABLE pads (
    id             INTEGER PRIMARY KEY,
    date           TEXT NOT NULL,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    source_account INTEGER NOT NULL REFERENCES accounts(id),
    consumed_by    INTEGER REFERENCES balance_assertions(id),
    generated_txn  INTEGER REFERENCES transactions(id)
);
CREATE INDEX idx_pads_account_date ON pads(account_id, date);
