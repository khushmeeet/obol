-- M5: append-only enforcement (design §11, plan §7).
--
-- No legitimate code path updates or deletes committed transactions or
-- postings — corrections are new transactions (reverse/replace) — so the
-- database itself now refuses such writes, for every connection, including
-- tools that bypass the repository layer. balance_assertions, pads, and
-- accounts keep their narrow update paths (check results, pad consumption,
-- account closing) and stay trigger-free.
--
-- The triggers are a guard against accidents, not against a determined
-- operator: forensic tooling that must rewrite history (the corruption-
-- injection tests do exactly that) drops them first.

CREATE TRIGGER transactions_append_only_update
BEFORE UPDATE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are append-only; corrections are new transactions (reverse/replace)');
END;

CREATE TRIGGER transactions_append_only_delete
BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are append-only; corrections are new transactions (reverse/replace)');
END;

CREATE TRIGGER postings_append_only_update
BEFORE UPDATE ON postings
BEGIN
    SELECT RAISE(ABORT, 'postings are append-only; corrections are new transactions (reverse/replace)');
END;

CREATE TRIGGER postings_append_only_delete
BEFORE DELETE ON postings
BEGIN
    SELECT RAISE(ABORT, 'postings are append-only; corrections are new transactions (reverse/replace)');
END;
