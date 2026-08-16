-- M6: tags, links, and the hub attachments (design §10, plan §8).
--
-- Tags and links are part of the committed transaction record, written by
-- record() alongside the postings and never rewritten afterwards — the
-- append-only triggers below extend migration 0003's invariant to them.
-- Re-tagging a committed transaction is a correction (replace()).
--
-- transaction_tags is indexed for the tag -> transactions -> postings ->
-- accounts traversal (plan §8): tag queries slice across the account
-- hierarchy, so the join table must be enterable from the tag side.
--
-- notes, documents, and events are hub attachments, not accounting
-- records: small tables, no update path in the repository, but no
-- triggers either — whether the product may prune them is left open.
-- Documents store a path and content hash only; the library never
-- manages file storage (plan §8).
--
-- Divergence from design §10 recorded here: notes.account_id and
-- documents.account_id are NOT NULL. Beancount's note and document
-- directives require an account, and an account-less dated fact is what
-- the events table is for.

CREATE TABLE tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE transaction_tags (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    tag_id         INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (transaction_id, tag_id)
);
CREATE INDEX idx_txn_tags_tag ON transaction_tags(tag_id, transaction_id);

CREATE TABLE links (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    name           TEXT NOT NULL,
    PRIMARY KEY (transaction_id, name)
);
CREATE INDEX idx_links_name ON links(name, transaction_id);

CREATE TABLE notes (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    comment    TEXT NOT NULL
);
CREATE INDEX idx_notes_account_date ON notes(account_id, date);

CREATE TABLE documents (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    path       TEXT NOT NULL,
    sha256     TEXT
);
CREATE INDEX idx_documents_account_date ON documents(account_id, date);

CREATE TABLE events (
    id    INTEGER PRIMARY KEY,
    date  TEXT NOT NULL,
    type  TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE INDEX idx_events_type_date ON events(type, date);

CREATE TRIGGER tags_append_only_update
BEFORE UPDATE ON tags
BEGIN
    SELECT RAISE(ABORT, 'tags are append-only; renaming a tag would rewrite committed transactions');
END;

CREATE TRIGGER tags_append_only_delete
BEFORE DELETE ON tags
BEGIN
    SELECT RAISE(ABORT, 'tags are append-only; renaming a tag would rewrite committed transactions');
END;

CREATE TRIGGER transaction_tags_append_only_update
BEFORE UPDATE ON transaction_tags
BEGIN
    SELECT RAISE(ABORT, 'transaction tags are append-only; re-tagging is a correction (replace)');
END;

CREATE TRIGGER transaction_tags_append_only_delete
BEFORE DELETE ON transaction_tags
BEGIN
    SELECT RAISE(ABORT, 'transaction tags are append-only; re-tagging is a correction (replace)');
END;

CREATE TRIGGER links_append_only_update
BEFORE UPDATE ON links
BEGIN
    SELECT RAISE(ABORT, 'links are append-only; re-linking is a correction (replace)');
END;

CREATE TRIGGER links_append_only_delete
BEFORE DELETE ON links
BEGIN
    SELECT RAISE(ABORT, 'links are append-only; re-linking is a correction (replace)');
END;
