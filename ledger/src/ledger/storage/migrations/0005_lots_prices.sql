-- M7 (design §7, §9, §10): lots, lot_reductions, prices, and the cost /
-- price columns' recorded precisions.
--
-- Deviations from the design §10 baseline, all deliberate:
--   * postings gain cost_precision / price_precision, lots gain
--     cost_precision, prices gain price_precision — every stored amount
--     carries the number of decimals as written (design §5), and these
--     columns had nowhere to record it. Tolerance re-inference and
--     precision-faithful export both need them.
--   * lot_reductions.quantity is signed: positive consumes the lot,
--     negative restores it (a reversal of a lot-reducing posting inserts
--     compensating rows rather than deleting history — design §11 under
--     the append-only rule).
--
-- A posting's written cost spec maps onto the cost_* columns; an empty
-- reduction filter {} leaves all four NULL and is recognisable by the
-- posting having lot_reductions rows.

ALTER TABLE postings ADD COLUMN cost_precision INTEGER;
ALTER TABLE postings ADD COLUMN price_precision INTEGER;

CREATE TABLE lots (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    commodity_id      INTEGER NOT NULL REFERENCES commodities(id),
    acquired_on       TEXT NOT NULL,             -- the lot date (cost identity)
    original_quantity INTEGER NOT NULL,          -- scale 8, positive
    cost_per_unit     INTEGER NOT NULL,          -- scale 8
    cost_precision    INTEGER NOT NULL,          -- decimals as written
    cost_commodity    INTEGER NOT NULL REFERENCES commodities(id),
    label             TEXT,
    opened_by_posting INTEGER NOT NULL REFERENCES postings(id),
    CHECK (original_quantity > 0),
    CHECK (cost_per_unit >= 0)
);
CREATE INDEX idx_lots_lookup
    ON lots(account_id, commodity_id, acquired_on);

CREATE TABLE lot_reductions (
    id         INTEGER PRIMARY KEY,
    lot_id     INTEGER NOT NULL REFERENCES lots(id),
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    quantity   INTEGER NOT NULL,    -- scale 8; positive consumes, negative restores
    CHECK (quantity != 0)
);
CREATE INDEX idx_lotred_lot ON lot_reductions(lot_id);
CREATE INDEX idx_lotred_posting ON lot_reductions(posting_id);

CREATE TABLE prices (
    id              INTEGER PRIMARY KEY,
    date            TEXT NOT NULL,
    commodity_id    INTEGER NOT NULL REFERENCES commodities(id),
    price           INTEGER NOT NULL,            -- scale 8, per unit
    price_precision INTEGER NOT NULL,            -- decimals as written
    quote_commodity INTEGER NOT NULL REFERENCES commodities(id),
    origin          TEXT CHECK (origin IN ('directive','transaction','fetch')),
    UNIQUE (date, commodity_id, quote_commodity)
);
CREATE INDEX idx_prices_lookup
    ON prices(commodity_id, quote_commodity, date DESC);

-- Booking is resolved once and stored (design §16); lots and their
-- reductions are as append-only as the postings they mirror. Prices stay
-- trigger-free: they are market data with an explicit upsert path, not
-- accounting records.
CREATE TRIGGER lots_append_only_update
BEFORE UPDATE ON lots
BEGIN
    SELECT RAISE(ABORT, 'lots are append-only; corrections are reversals');
END;

CREATE TRIGGER lots_append_only_delete
BEFORE DELETE ON lots
BEGIN
    SELECT RAISE(ABORT, 'lots are append-only; corrections are reversals');
END;

CREATE TRIGGER lot_reductions_append_only_update
BEFORE UPDATE ON lot_reductions
BEGIN
    SELECT RAISE(ABORT, 'lot_reductions are append-only; a reversal inserts compensating rows');
END;

CREATE TRIGGER lot_reductions_append_only_delete
BEFORE DELETE ON lot_reductions
BEGIN
    SELECT RAISE(ABORT, 'lot_reductions are append-only; a reversal inserts compensating rows');
END;
