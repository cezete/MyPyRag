"""Deterministic, workspace-scoped workflows for less reliable tool-using models."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Literal

ProbeMode = Literal["fail_if_exists", "overwrite_allowed"]

log = logging.getLogger(__name__)

TOOL_NAME = "run_evidence_file_probe"
MAX_WORKFLOW_STEPS = 8
_ALLOWED_MODES = {"fail_if_exists", "overwrite_allowed"}
_PROTECTED_PARTS = {".git", ".hg", ".svn"}


class EvidenceFileProbe:
    """Run the complete evidence-file workflow without exposing atomic file tools."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()
        self._invocations: dict[str, int] = {}
        self._invocations_lock = threading.Lock()
        self._workflow_lock = threading.Lock()

    def run(
        self,
        target_path: str,
        initial_content: str,
        append_content: str,
        mode: ProbeMode,
        verify: bool,
    ) -> dict[str, Any]:
        # A single server process serializes this intentionally small diagnostic workflow.
        # This prevents two calls targeting the same path from interleaving their write/append.
        with self._workflow_lock:
            return self._run_locked(
                target_path, initial_content, append_content, mode, verify
            )

    def _run_locked(
        self,
        target_path: str,
        initial_content: str,
        append_content: str,
        mode: ProbeMode,
        verify: bool,
    ) -> dict[str, Any]:
        fingerprint, invocation_count = self._record_invocation(
            target_path, initial_content, append_content, mode, verify
        )
        diagnostics = {
            "call_fingerprint": fingerprint,
            "invocation_count": invocation_count,
            "repeated_call": invocation_count > 1,
            "max_workflow_steps": MAX_WORKFLOW_STEPS,
        }
        steps: list[dict[str, str]] = []

        if mode not in _ALLOWED_MODES:
            return self._rejected(
                steps,
                diagnostics,
                reason="invalid_mode",
                message="A mode csak fail_if_exists vagy overwrite_allowed lehet.",
            )
        if not isinstance(verify, bool):
            return self._rejected(
                steps,
                diagnostics,
                reason="invalid_verify",
                message="A verify értékének true vagy false logikai értéknek kell lennie.",
            )

        try:
            target = self._safe_target(target_path)
        except ValueError as exc:
            self._step(steps, "validate_target_path", "FAIL", str(exc))
            return self._rejected(
                steps,
                diagnostics,
                reason="invalid_path",
                message=(
                    "A target_path csak relatív, workspace-en belüli, nem linkelt fájlútvonal "
                    "lehet. Válassz biztonságos célútvonalat."
                ),
            )

        self._step(
            steps,
            "validate_target_path",
            "OK",
            f"Workspace-en belüli cél: {target.relative_to(self._workspace_root).as_posix()}",
        )
        if mode == "fail_if_exists" and target.exists():
            self._step(steps, "check_existing_target", "FAIL", "A célfájl már létezik.")
            return self._rejected(
                steps,
                diagnostics,
                reason="target_already_exists",
                message=(
                    "A célfájl már létezik. Válassz másik target_path értéket, vagy használd "
                    "az overwrite_allowed mode-ot."
                ),
                target=target,
            )
        self._step(steps, "check_existing_target", "OK", "A létrehozási mód alkalmazható.")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Re-check after directory creation so a pre-existing symlink cannot redirect writes.
            target = self._safe_target(target_path)
            self._write_initial(target, initial_content, exclusive=mode == "fail_if_exists")
            self._step(steps, "write_initial_content", "OK", "A kezdeti tartalom kiírva.")

            if verify:
                initial_readback = self._read_text(target)
                self._step(steps, "read_initial_content", "OK", "A kezdeti tartalom visszaolvasva.")
                if initial_readback != initial_content:
                    self._step(
                        steps,
                        "verify_initial_content",
                        "FAIL",
                        "A visszaolvasott kezdeti tartalom nem egyezik.",
                    )
                    return self._failed(
                        steps,
                        diagnostics,
                        reason="initial_content_mismatch",
                        message="A kezdeti visszaellenőrzés eltérést talált; vizsgáld meg a fájlrendszert.",
                        target=target,
                    )
                self._step(
                    steps, "verify_initial_content", "OK", "A kezdeti tartalom pontosan egyezik."
                )
            else:
                self._step(steps, "read_initial_content", "SKIPPED", "verify=false")
                self._step(steps, "verify_initial_content", "SKIPPED", "verify=false")

            self._append_text(target, append_content)
            self._step(steps, "append_content", "OK", "A hozzáfűzendő tartalom kiírva.")

            if verify:
                final_readback = self._read_text(target)
                self._step(steps, "read_final_content", "OK", "A végső tartalom visszaolvasva.")
                if final_readback != initial_content + append_content:
                    self._step(
                        steps,
                        "verify_final_content",
                        "FAIL",
                        "A végső tartalom nem egyezik az elvárt értékkel.",
                    )
                    return self._failed(
                        steps,
                        diagnostics,
                        reason="final_content_mismatch",
                        message="A végső visszaellenőrzés eltérést talált; vizsgáld meg a fájlrendszert.",
                        target=target,
                    )
                self._step(steps, "verify_final_content", "OK", "A végső tartalom pontosan egyezik.")
            else:
                self._step(steps, "read_final_content", "SKIPPED", "verify=false")
                self._step(steps, "verify_final_content", "SKIPPED", "verify=false")
        except FileExistsError:
            self._step(steps, "write_initial_content", "FAIL", "A célfájl időközben létrejött.")
            return self._rejected(
                steps,
                diagnostics,
                reason="target_already_exists",
                message=(
                    "A célfájl már létezik. Válassz másik target_path értéket, vagy használd "
                    "az overwrite_allowed mode-ot."
                ),
                target=target,
            )
        except (OSError, UnicodeError) as exc:
            self._step(steps, "file_operation", "FAIL", f"{type(exc).__name__}: {exc}")
            return self._failed(
                steps,
                diagnostics,
                reason="file_operation_failed",
                message="A fájlművelet meghiúsult; ellenőrizd a célútvonalat és a jogosultságokat.",
                target=target,
            )

        return {
            "status": "PASS",
            "tool": TOOL_NAME,
            "steps": steps,
            "final_state": self._final_state(target),
            "diagnostics": diagnostics,
            "next_allowed_actions": ["finish", "inspect_project_state"],
        }

    def _safe_target(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
            raise ValueError("A target_path üres vagy érvénytelen.")
        candidate_input = Path(raw_path)
        if candidate_input.is_absolute() or candidate_input.anchor:
            raise ValueError("Abszolút target_path nem engedélyezett.")
        if any(part.casefold() in _PROTECTED_PARTS for part in candidate_input.parts):
            raise ValueError("Verziókezelő meta-könyvtár nem módosítható.")

        unresolved = self._workspace_root
        for part in candidate_input.parts:
            unresolved = unresolved / part
            if unresolved.is_symlink():
                raise ValueError("Szimbolikus linken vagy junctionön át történő írás tiltott.")

        candidate = (self._workspace_root / candidate_input).resolve(strict=False)
        if candidate == self._workspace_root or self._workspace_root not in candidate.parents:
            raise ValueError("A target_path kilépne a workspace-ből.")
        if candidate.exists() and not candidate.is_file():
            raise ValueError("A target_path nem normál fájlra mutat.")
        return candidate

    @staticmethod
    def _write_initial(path: Path, content: str, *, exclusive: bool) -> None:
        with path.open("x" if exclusive else "w", encoding="utf-8", newline="") as handle:
            handle.write(content)

    @staticmethod
    def _append_text(path: Path, content: str) -> None:
        with path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(content)

    @staticmethod
    def _read_text(path: Path) -> str:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def _record_invocation(
        self,
        target_path: str,
        initial_content: str,
        append_content: str,
        mode: str,
        verify: object,
    ) -> tuple[str, int]:
        canonical = json.dumps(
            {
                "target_path": target_path,
                "initial_content": initial_content,
                "append_content": append_content,
                "mode": mode,
                "verify": verify,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = hashlib.sha256(canonical).hexdigest()
        with self._invocations_lock:
            count = self._invocations.get(fingerprint, 0) + 1
            self._invocations[fingerprint] = count
        return fingerprint, count

    @staticmethod
    def _step(steps: list[dict[str, str]], name: str, status: str, evidence: str) -> None:
        if len(steps) >= MAX_WORKFLOW_STEPS:
            raise RuntimeError("workflow step limit exceeded")
        step = {"name": name, "status": status, "evidence": evidence[:300]}
        steps.append(step)
        log.info("qwen_terelo_step %s", json.dumps(step, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _final_state(target: Path | None) -> dict[str, Any]:
        if target is None or not target.exists() or not target.is_file():
            return {"path": str(target) if target is not None else None, "exists": False}
        try:
            data = target.read_bytes()
        except OSError as exc:
            return {
                "path": str(target),
                "exists": True,
                "state_error": f"{type(exc).__name__}: {exc}"[:300],
            }
        return {
            "path": str(target),
            "exists": True,
            "content_sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }

    def _rejected(
        self,
        steps: list[dict[str, str]],
        diagnostics: dict[str, Any],
        *,
        reason: str,
        message: str,
        target: Path | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "REJECTED",
            "tool": TOOL_NAME,
            "reason": reason,
            "message": message,
            "steps": steps,
            "final_state": self._final_state(target),
            "diagnostics": diagnostics,
            "next_allowed_actions": [TOOL_NAME, "ask_user", "finish"],
        }

    def _failed(
        self,
        steps: list[dict[str, str]],
        diagnostics: dict[str, Any],
        *,
        reason: str,
        message: str,
        target: Path,
    ) -> dict[str, Any]:
        return {
            "status": "FAIL",
            "tool": TOOL_NAME,
            "reason": reason,
            "message": message,
            "steps": steps,
            "final_state": self._final_state(target),
            "diagnostics": diagnostics,
            "next_allowed_actions": ["inspect_project_state", "ask_user", "finish"],
        }
