import hashlib
import os

import pytest

from mypyrag.config import Config
from mypyrag.qwen_terelo import EvidenceFileProbe


def test_workspace_directory_resolves_from_config_base(tmp_path, monkeypatch):
    workspace = tmp_path / "workflow-sandbox"
    workspace.mkdir()
    monkeypatch.setenv("MYPYRAG_MCP_WORKSPACE_DIR", "workflow-sandbox")

    assert Config.load(tmp_path).mcp_workspace_dir == workspace


def test_successful_verified_probe_returns_evidence_and_exact_bytes(tmp_path):
    probe = EvidenceFileProbe(tmp_path)

    result = probe.run("evidence/probe.txt", "első\r\n", "második ✓", "fail_if_exists", True)

    expected = "első\r\nmásodik ✓".encode()
    assert result["status"] == "PASS"
    assert [step["status"] for step in result["steps"]] == ["OK"] * 8
    assert (tmp_path / "evidence" / "probe.txt").read_bytes() == expected
    assert result["final_state"]["content_sha256"] == hashlib.sha256(expected).hexdigest()
    assert result["final_state"]["size_bytes"] == len(expected)
    assert result["next_allowed_actions"] == ["finish", "inspect_project_state"]


def test_existing_target_is_rejected_without_modification(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("keep", encoding="utf-8")
    probe = EvidenceFileProbe(tmp_path)

    result = probe.run("existing.txt", "replace", "append", "fail_if_exists", True)

    assert result["status"] == "REJECTED"
    assert result["reason"] == "target_already_exists"
    assert target.read_text(encoding="utf-8") == "keep"
    assert "overwrite_allowed" in result["message"]


@pytest.mark.parametrize("target", ["../outside.txt", ".git/config"])
def test_unsafe_target_is_rejected(tmp_path, target):
    result = EvidenceFileProbe(tmp_path).run(target, "a", "b", "fail_if_exists", True)

    assert result["status"] == "REJECTED"
    assert result["reason"] == "invalid_path"
    assert result["steps"][0]["name"] == "validate_target_path"


def test_absolute_target_is_rejected(tmp_path):
    result = EvidenceFileProbe(tmp_path).run(
        str(tmp_path / "absolute.txt"), "a", "b", "fail_if_exists", True
    )

    assert result["status"] == "REJECTED"
    assert result["reason"] == "invalid_path"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
def test_symlink_target_is_rejected(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(real_dir, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted")

    result = EvidenceFileProbe(tmp_path).run(
        "linked/probe.txt", "a", "b", "fail_if_exists", True
    )

    assert result["status"] == "REJECTED"
    assert result["reason"] == "invalid_path"


def test_readback_mismatch_is_reported_as_fail(tmp_path, monkeypatch):
    probe = EvidenceFileProbe(tmp_path)
    monkeypatch.setattr(probe, "_read_text", lambda _path: "changed externally")

    result = probe.run("probe.txt", "initial", "append", "fail_if_exists", True)

    assert result["status"] == "FAIL"
    assert result["reason"] == "initial_content_mismatch"
    assert result["steps"][-1]["status"] == "FAIL"
    assert result["final_state"]["exists"] is True


def test_identical_repeated_call_is_detected(tmp_path):
    probe = EvidenceFileProbe(tmp_path)
    arguments = ("probe.txt", "initial", "append", "overwrite_allowed", True)

    first = probe.run(*arguments)
    second = probe.run(*arguments)

    assert first["status"] == second["status"] == "PASS"
    assert first["diagnostics"]["repeated_call"] is False
    assert second["diagnostics"]["repeated_call"] is True
    assert second["diagnostics"]["invocation_count"] == 2
    assert first["diagnostics"]["call_fingerprint"] == second["diagnostics"]["call_fingerprint"]


def test_verify_false_marks_checks_skipped_but_writes_expected_content(tmp_path):
    result = EvidenceFileProbe(tmp_path).run(
        "probe.txt", "initial", "append", "fail_if_exists", False
    )

    assert result["status"] == "PASS"
    assert sum(step["status"] == "SKIPPED" for step in result["steps"]) == 4
    assert (tmp_path / "probe.txt").read_text(encoding="utf-8") == "initialappend"
