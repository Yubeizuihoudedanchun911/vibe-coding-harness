from __future__ import annotations

import dataclasses
from pathlib import Path, PurePosixPath

from vibe.models import (
    ContractError,
    EvaluationCriterion,
    EvaluationFinding,
    EvaluationResult,
    EvaluationVerdict,
    FrozenRunConfig,
)
from vibe.prompt_registry import (
    PromptRegistry,
    parse_single_json_object,
)
from vibe.providers.base import (
    ProviderAdapter,
    ProviderHandle,
)
from vibe.runners import (
    DispatchLedger,
    ReadOnlyAudit,
    RoleInvocation,
    require_operation_id,
)
from vibe.runners.planner import (
    _canonical_id,
    _exact_object,
    _list,
    _nonempty_string,
    _nonnegative_int,
    _positive_int,
    _relative_path,
    _relative_worktree,
    _unique_strings,
)
from vibe.state_store import canonical_json_bytes


class EvaluatorRunner:
    def __init__(
        self,
        *,
        registry: PromptRegistry,
        provider: ProviderAdapter,
        target_root: Path,
        run_root: Path,
        expected_base: str,
        config: FrozenRunConfig,
        config_sha256: str,
        read_only_audit: ReadOnlyAudit,
    ) -> None:
        self.registry = registry
        self.provider = provider
        self.target_root = target_root.resolve()
        self.run_root = run_root
        self.expected_base = expected_base
        self.config = config
        self.config_sha256 = config_sha256
        self.read_only_audit = read_only_audit
        self._audits: dict[
            str,
            tuple[dict[str, object], RoleInvocation],
        ] = {}

    def prepare(
        self,
        *,
        run_id: str,
        operation_id: str,
        attempt_no: int,
        attempt_created_at: str,
        attempt_token: str,
        worktree: Path,
        context: dict[str, object],
        artifact_prefix: str,
        provider_retry_no: int = 0,
        timeout_seconds: int = 900,
    ) -> RoleInvocation:
        del run_id
        require_operation_id(operation_id)
        relative_worktree = _relative_worktree(
            worktree,
            self.target_root,
        )
        enriched = {
            **context,
            "authorized_command_ids": [
                {
                    "id": command.id,
                    "purpose": command.purpose,
                }
                for command in self.config.command_catalog
            ],
            "required_command_ids": list(
                self.config.required_command_ids
            ),
            "config_sha256": self.config_sha256,
        }
        rendered = self.registry.compose_evaluator(enriched)
        execution = self.provider.execution_identity()
        return RoleInvocation(
            role="evaluator",
            task_id=None,
            operation_id=operation_id,
            attempt_no=_positive_int(
                attempt_no,
                "attempt_no",
            ),
            attempt_created_at=attempt_created_at,
            attempt_token=attempt_token,
            provider_retry_no=_nonnegative_int(
                provider_retry_no,
                "provider_retry_no",
            ),
            expected_base=self.expected_base,
            branch=None,
            worktree=relative_worktree,
            target_root=str(self.target_root),
            run_root=str(self.run_root),
            prompt_body=rendered.body,
            prompt_versions=rendered.prompts,
            schema_body=rendered.schema_path.read_bytes(),
            preflight_body=canonical_json_bytes(
                {
                    "role": "evaluator",
                    "attempt_created_at": attempt_created_at,
                    "expected_base": self.expected_base,
                    "worktree": relative_worktree,
                }
            ),
            authorized_command_ids=tuple(
                command.id
                for command in self.config.command_catalog
            ),
            required_command_ids=(
                self.config.required_command_ids
            ),
            config_sha256=self.config_sha256,
            codex_version=execution.codex_version,
            execution_policy_sha256=execution.policy_sha256,
            sandbox="read-only",
            artifact_prefix=artifact_prefix,
            timeout_seconds=_positive_int(
                timeout_seconds,
                "timeout_seconds",
            ),
        )

    def start(
        self,
        invocation: RoleInvocation,
        ledger: DispatchLedger,
    ) -> ProviderHandle:
        worktree = Path(invocation.target_root).joinpath(
            *PurePosixPath(invocation.worktree).parts
        )
        before = self.read_only_audit.capture(worktree)
        audited = dataclasses.replace(
            invocation,
            preflight_body=canonical_json_bytes(
                {
                    "role": invocation.role,
                    "attempt_created_at": (
                        invocation.attempt_created_at
                    ),
                    "expected_base": invocation.expected_base,
                    "worktree": invocation.worktree,
                    "audit": before,
                }
            ),
        )
        self._audits[invocation.attempt_token] = (
            before,
            audited,
        )
        return ledger.dispatch(audited, self.provider.start)

    def result(
        self,
        invocation: RoleInvocation,
        handle: ProviderHandle,
        ledger: DispatchLedger,
    ) -> EvaluationResult:
        before, audited = self._audits.get(
            invocation.attempt_token,
            ({}, invocation),
        )
        ledger.bind_completion(audited, handle)
        if before:
            worktree = Path(audited.target_root).joinpath(
                *PurePosixPath(audited.worktree).parts
            )
            after = self.read_only_audit.capture(worktree)
            self.read_only_audit.assert_unchanged(
                before,
                after,
            )
        return self.parse_result(
            self.provider.result(handle).body
        )

    def parse_result(
        self,
        body: bytes,
        *,
        expected_criteria: tuple[str, ...] | None = None,
        authorized_command_ids: tuple[str, ...] | None = None,
    ) -> EvaluationResult:
        raw = _exact_object(
            parse_single_json_object(body),
            "evaluation",
            {
                "schema_version",
                "verdict",
                "criteria",
                "findings",
                "evidence_requests",
                "residual_risks",
            },
        )
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
        ):
            raise ContractError(
                "evaluation schema_version is invalid"
            )
        try:
            verdict = EvaluationVerdict(raw["verdict"])
        except (TypeError, ValueError) as error:
            raise ContractError(
                "evaluation verdict is invalid"
            ) from error
        criteria: list[EvaluationCriterion] = []
        criterion_ids: set[str] = set()
        for value in _list(raw["criteria"], "criteria"):
            item = _exact_object(
                value,
                "evaluation criterion",
                {"id", "verdict", "evidence_ids"},
            )
            criterion_id = _canonical_id(
                item["id"],
                "AC-",
                "criterion ID",
            )
            if criterion_id in criterion_ids:
                raise ContractError(
                    "evaluation criteria must be unique"
                )
            criterion_ids.add(criterion_id)
            criterion_verdict = item["verdict"]
            if criterion_verdict not in {
                "PASS",
                "FAIL",
                "UNVERIFIED",
                "BLOCKED",
            }:
                raise ContractError(
                    "criterion verdict is invalid"
                )
            criteria.append(
                EvaluationCriterion(
                    id=criterion_id,
                    verdict=criterion_verdict,
                    evidence_ids=_unique_strings(
                        item["evidence_ids"],
                        "criterion evidence_ids",
                    ),
                )
            )
        if (
            expected_criteria is not None
            and criterion_ids != set(expected_criteria)
        ):
            raise ContractError(
                "evaluation criteria do not match acceptance criteria"
            )
        findings: list[EvaluationFinding] = []
        for value in _list(raw["findings"], "findings"):
            item = _exact_object(
                value,
                "evaluation finding",
                {
                    "criterion_id",
                    "severity",
                    "evidence",
                    "affected_paths",
                    "repair_hint",
                },
            )
            criterion_id = _canonical_id(
                item["criterion_id"],
                "AC-",
                "finding criterion ID",
            )
            if criterion_id not in criterion_ids:
                raise ContractError(
                    "finding references an unknown criterion"
                )
            severity = item["severity"]
            if severity not in {
                "LOW",
                "MEDIUM",
                "HIGH",
                "CRITICAL",
            }:
                raise ContractError(
                    "finding severity is invalid"
                )
            paths = _unique_strings(
                item["affected_paths"],
                "finding affected_paths",
            )
            for path in paths:
                _relative_path(path, "finding affected path")
            findings.append(
                EvaluationFinding(
                    criterion_id=criterion_id,
                    severity=severity,
                    evidence=_nonempty_string(
                        item["evidence"],
                        "finding evidence",
                    ),
                    affected_paths=paths,
                    repair_hint=_nonempty_string(
                        item["repair_hint"],
                        "repair_hint",
                    ),
                )
            )
        requests = _unique_strings(
            raw["evidence_requests"],
            "evidence_requests",
        )
        if verdict is not EvaluationVerdict.UNVERIFIED and requests:
            raise ContractError(
                "evidence requests are allowed only for UNVERIFIED"
            )
        allowed = (
            set(authorized_command_ids)
            if authorized_command_ids is not None
            else {
                command.id
                for command in self.config.command_catalog
            }
        )
        if not set(requests).issubset(allowed):
            raise ContractError(
                "evaluation requested an unauthorized command ID"
            )
        if verdict is EvaluationVerdict.PASS:
            if findings or any(
                criterion.verdict != "PASS"
                or not criterion.evidence_ids
                for criterion in criteria
            ):
                raise ContractError(
                    "PASS requires direct evidence for every criterion"
                )
        residual = _unique_strings(
            raw["residual_risks"],
            "residual_risks",
        )
        return EvaluationResult(
            schema_version=1,
            verdict=verdict,
            criteria=tuple(criteria),
            findings=tuple(findings),
            evidence_requests=requests,
            residual_risks=residual,
        )
