import json
import os
from dataclasses import replace
from pathlib import PurePosixPath, PureWindowsPath
from unittest.mock import patch

import pytest
from test_pipeline import FakeConverter
from test_stage_three import FakeProvider, FakeStore

from mypyrag.chunker import ChunkingSpec, RawChunk
from mypyrag.cli import main
from mypyrag.config import Config
from mypyrag.indexing import IndexingService, UniverseSummary
from mypyrag.model import MaxStage, State
from mypyrag.pipeline import Pipeline
from mypyrag.universe import UniversePolicy


@pytest.fixture
def inbox_config(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith("MYPYRAG_"):
            monkeypatch.delenv(key)
    return replace(
        Config.load(tmp_path),
        max_stage=MaxStage.CONVERTED,
        file_stable_seconds=0,
    )


@pytest.fixture
def inbox_pipeline(inbox_config):
    return Pipeline(inbox_config, FakeConverter())


@pytest.mark.parametrize(
    "parts,expected",
    [
        (("retro",), "retro"),
        (("retro", "c64"), "retro.c64"),
        (("retro", "c64", "sid"), "retro.c64.sid"),
        (PurePosixPath("retro/c64").parts, "retro.c64"),
        (PureWindowsPath(r"retro\c64").parts, "retro.c64"),
    ],
)
def test_universe_from_platform_independent_parts(parts, expected):
    assert UniversePolicy().from_parts(parts) == expected


@pytest.mark.parametrize(
    "value,reason",
    [
        ("", "empty"),
        ("Retro", "must match"),
        ("zx spectrum", "must match"),
        ("retro..c64", "empty segment"),
        ("../retro", "empty segment"),
        ("n.a.", "not indexable"),
    ],
)
def test_universe_syntax_rejections(value, reason):
    with pytest.raises(ValueError, match=reason):
        UniversePolicy().validate(value)


def test_universe_limits():
    with pytest.raises(ValueError, match="segment"):
        UniversePolicy(segment_max_length=3).validate("retro")
    with pytest.raises(ValueError, match="length"):
        UniversePolicy(max_length=5).validate("retro.c64")
    with pytest.raises(ValueError, match="depth"):
        UniversePolicy(max_depth=2).validate("retro.c64.sid")


def test_dot_is_forbidden_inside_a_directory_segment():
    with pytest.raises(ValueError, match="must match"):
        UniversePolicy().from_parts(("c64.docs",))


def test_recursive_watcher_sets_universe_on_first_manifest_and_prunes_workdir(
    inbox_pipeline,
):
    source_dir = inbox_pipeline.config.in_dir / "retro" / "c64"
    source_dir.mkdir(parents=True)
    source = source_dir / "test.htm"
    source.write_text("<html><body>test</body></html>", encoding="utf-8")

    assert inbox_pipeline.cycle()
    [(workdir, manifest)] = inbox_pipeline.manifests()
    first_payload = json.loads((workdir / "manifest.json").read_text(encoding="utf-8"))
    assert first_payload["universe"] == "retro.c64"
    assert manifest.current_state == State.CONVERTED
    assert manifest.original_filename == "test.htm"
    assert not source.exists()
    assert (workdir / "source" / "test.htm").exists()

    assert inbox_pipeline.cycle()
    assert len(inbox_pipeline.manifests()) == 1


def test_hierarchical_watcher_reaches_done_without_manual_universe(inbox_config):
    class OneChunk:
        spec = ChunkingSpec.create(inbox_config)

        def chunks(self, document_json):
            return [
                RawChunk(
                    "text",
                    "body",
                    "Commodore memory map",
                    ["Memory"],
                    ["Memory"],
                    [1],
                    ["#/texts/0"],
                )
            ]

    config = replace(inbox_config, max_stage=MaxStage.INDEXED)
    provider, store = FakeProvider(), FakeStore()
    pipeline = Pipeline(
        config,
        FakeConverter(),
        chunker=OneChunk(),
        indexer=IndexingService(config, provider, store),
    )
    source_dir = config.in_dir / "retro" / "c64"
    source_dir.mkdir(parents=True)
    (source_dir / "manual.htm").write_text("manual")

    assert pipeline.cycle()
    [(directory, manifest)] = pipeline.manifests()
    assert directory.parent == config.done_dir
    assert manifest.universe == "retro.c64"
    assert manifest.current_state == State.DONE
    assert store.replacements[0][1] == "retro.c64"
    assert pipeline.cycle()
    assert len(pipeline.manifests()) == 1


def test_hierarchical_watcher_runs_fake_pipeline_to_done(inbox_config):
    class OneChunk:
        spec = ChunkingSpec.create(inbox_config)

        def chunks(self, document_json):
            return [RawChunk("text", "body", "content", [], [], [], [])]

    config = replace(inbox_config, max_stage=MaxStage.INDEXED)
    provider, store = FakeProvider(), FakeStore()
    pipeline = Pipeline(
        config,
        FakeConverter(),
        chunker=OneChunk(),
        indexer=IndexingService(config, provider, store),
    )
    source_dir = config.in_dir / "retro" / "c64"
    source_dir.mkdir(parents=True)
    (source_dir / "test.htm").write_text("content")

    assert pipeline.cycle()
    [(directory, manifest)] = pipeline.manifests()
    assert directory.parent == config.done_dir
    assert manifest.current_state == State.DONE
    assert manifest.universe == "retro.c64"
    assert len(store.replacements) == 1
    assert pipeline.cycle()
    assert len(store.replacements) == 1


def test_watcher_finds_parent_and_child_universes_together(inbox_pipeline):
    retro = inbox_pipeline.config.in_dir / "retro"
    c64 = retro / "c64"
    c64.mkdir(parents=True)
    (retro / "history.md").write_text("history")
    (c64 / "memory.md").write_text("memory")

    discovered = inbox_pipeline._discover_inputs()
    assert discovered[retro / "history.md"] == "retro"
    assert discovered[c64 / "memory.md"] == "retro.c64"


def test_root_input_is_ignored_and_warning_is_deduplicated(inbox_pipeline, caplog):
    source = inbox_pipeline.config.in_dir / "unclassified.md"
    source.write_text("content")
    assert inbox_pipeline.cycle()
    assert inbox_pipeline.cycle()
    assert source.exists()
    assert inbox_pipeline.manifests() == []
    messages = [record.message for record in caplog.records if "Unclassified input" in record.message]
    assert len(messages) == 1


def test_invalid_universe_does_not_block_valid_input(inbox_pipeline):
    invalid = inbox_pipeline.config.in_dir / "Bad Name"
    valid = inbox_pipeline.config.in_dir / "retro"
    invalid.mkdir()
    valid.mkdir()
    (invalid / "bad.md").write_text("bad")
    (valid / "good.md").write_text("good")
    assert inbox_pipeline.cycle()
    [(_, manifest)] = inbox_pipeline.manifests()
    assert manifest.original_filename == "good.md"
    assert manifest.universe == "retro"
    assert (invalid / "bad.md").exists()


def test_hidden_and_technical_files_are_ignored(inbox_pipeline):
    retro = inbox_pipeline.config.in_dir / "retro"
    hidden_dir = inbox_pipeline.config.in_dir / ".hidden"
    retro.mkdir()
    hidden_dir.mkdir()
    (retro / ".secret.md").write_text("secret")
    (retro / "upload.md.part").write_text("partial")
    (hidden_dir / "hidden.md").write_text("hidden")
    assert inbox_pipeline._discover_inputs() == {}


def test_document_work_directory_is_pruned_before_descending(inbox_pipeline):
    workdir = inbox_pipeline.config.in_dir / "valid-workdir"
    for child in ("source", "JSON", "CHUNKS"):
        (workdir / child).mkdir(parents=True, exist_ok=True)
    (workdir / "manifest.json").write_text("{}")
    (workdir / "source" / "original.htm").write_text("source")
    (workdir / "JSON" / "document.json").write_text("{}")
    (workdir / "CHUNKS" / "0001.json").write_text("{}")
    assert inbox_pipeline._discover_inputs() == {}


def test_process_derives_universe_inside_catalog_but_preserves_external_compatibility(
    inbox_pipeline, tmp_path
):
    nested = inbox_pipeline.config.in_dir / "java" / "spring"
    nested.mkdir(parents=True)
    source = nested / "reference.md"
    source.write_text("Spring")
    assert inbox_pipeline.process(source)
    [(_, manifest)] = inbox_pipeline.manifests()
    assert manifest.universe == "java.spring"

    external = tmp_path / "external.md"
    external.write_text("external")
    assert inbox_pipeline.process(external)
    manifests = {item.original_filename: item for _, item in inbox_pipeline.manifests()}
    assert manifests["external.md"].universe == "n.a."


def test_symlink_file_and_directory_are_not_followed(inbox_pipeline, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.md").write_text("outside")
    try:
        os.symlink(outside, inbox_pipeline.config.in_dir / "retro", target_is_directory=True)
        os.symlink(outside / "outside.md", inbox_pipeline.config.in_dir / "linked.md")
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    assert inbox_pipeline._discover_inputs() == {}


def test_same_filename_in_different_universes_has_distinct_stability_keys(inbox_pipeline):
    first = inbox_pipeline.config.in_dir / "retro" / "same.md"
    second = inbox_pipeline.config.in_dir / "python" / "same.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("one")
    second.write_text("two")
    discovered = inbox_pipeline._discover_inputs()
    inbox_pipeline.stability.prune(set(discovered))
    for path in discovered:
        inbox_pipeline.stability.ready(path, 0)
    assert set(inbox_pipeline.stability.observations) == {first, second}


class UniverseStore:
    def list_universes(self):
        return [
            UniverseSummary("java.spring", 1, 5),
            UniverseSummary("retro.c64", 2, 20),
        ]


def test_list_universes_cli_human_and_json(tmp_path, capsys):
    with patch("mypyrag.cli._index_store", return_value=UniverseStore()):
        assert main(["--base-dir", str(tmp_path), "list-universes"]) == 0
        human = capsys.readouterr().out
        assert "java.spring" in human and "retro.c64" in human
        assert main(["--base-dir", str(tmp_path), "list-universes", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
    assert payload[0] == {"universe": "java.spring", "documents": 1, "chunks": 5}


def test_list_universes_empty_database_is_successful(tmp_path, capsys):
    store = UniverseStore()
    store.list_universes = list
    with patch("mypyrag.cli._index_store", return_value=store):
        assert main(["--base-dir", str(tmp_path), "list-universes"]) == 0
    assert capsys.readouterr().out == "No indexed universes.\n"
