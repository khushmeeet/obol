"""Run every check, return a structured report (design §12).

Read-only: the validator recomputes and reports, it never repairs. Run it
after every ingestion batch, before every backup, and on demand from the
CLI (`ledger validate`).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from ledger.validation import checks
from ledger.validation.checks import Finding

if TYPE_CHECKING:
    from ledger.storage.repositories import Repository


@dataclass(frozen=True, slots=True)
class ValidationReport:
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def by_check(self) -> dict[str, list[Finding]]:
        grouped: dict[str, list[Finding]] = {}
        for finding in self.findings:
            grouped.setdefault(finding.check, []).append(finding)
        return grouped

    def __str__(self) -> str:
        if self.ok:
            return "ok: all checks passed"
        lines = [f"{len(self.findings)} finding(s):"]
        for finding in self.findings:
            lines.append(f"  [{finding.check}] {finding.message}")
        return "\n".join(lines)


def validate(repository: Repository) -> ValidationReport:
    transactions = repository.list_transactions()
    accounts = {account.path: account for account in repository.list_accounts()}
    assertions = repository.list_assertions()
    pads = repository.list_pads()
    lots = repository.list_lots()
    reductions = repository.list_lot_reductions()
    multiplier = Decimal(repository.get_option("inferred_tolerance_multiplier") or "1")

    lots_by_id = {lot.id: lot for lot in lots if lot.id is not None}
    lots_by_opening = {
        (lot.opened_by_transaction_id, lot.opened_by_seq): lot for lot in lots
    }
    findings = [
        *checks.check_transaction_balance(transactions, multiplier),
        *checks.check_minimum_postings(transactions),
        *checks.check_single_interpolation(transactions),
        *checks.check_account_lifetimes(transactions, accounts),
        *checks.check_allowed_commodities(transactions, accounts),
        *checks.check_weight_consistency(transactions, lots_by_id),
        *checks.check_lots(transactions, lots, reductions),
        *checks.check_global_balance(transactions, multiplier),
        *checks.check_assertions(repository, assertions, multiplier),
        *checks.check_generated_trace(transactions, pads, assertions),
        *checks.check_reversals(transactions, lots_by_opening),
        *checks.check_storage(repository),
    ]
    return ValidationReport(findings=tuple(findings))
