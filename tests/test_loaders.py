"""Acceptance tests for specs/01-ingestion-and-loaders.md (AC numbers in the test names)."""

import os
import shutil

import pytest

from src.config import IngestionSettings
from src.loaders import base as loader_base
from src.loaders.base import register
from src.loaders.vision import VisionDescriber
from src.pipeline.ingest import ingest_files
from src.pipeline.store import READY, SessionStore
from src.schema import Element, validate_element
from tests.conftest import FAKE_DESCRIPTION


def elements_of(batch, index=0):
    result = batch.files[index]
    assert result.status == "ok", result.reason
    store = SessionStore(batch.session_id)
    return [Element(**e) for e in store.read_elements(result.doc_id)]


def test_ac1_sessions_are_isolated(samples, vision, data_root):
    a = ingest_files([samples["csv"]], vision=vision)
    b = ingest_files([samples["csv"]], vision=vision)
    path_a, path_b = a.files[0].stored_path, b.files[0].stored_path
    assert a.session_id != b.session_id
    assert os.path.dirname(os.path.dirname(path_a)) != os.path.dirname(os.path.dirname(path_b))
    assert a.session_id in path_a and b.session_id in path_b
    for doc in SessionStore(b.session_id).documents().values():
        assert a.session_id not in (doc.get("stored_path") or "")


def test_ac2_new_loader_needs_only_a_registration(tmp_path, vision):
    @register(".xyz")
    def load_xyz(ctx):
        return [ctx.make("text", "xyz content", {"line_start": 1, "line_end": 1}, "lines 1-1")]

    try:
        path = tmp_path / "sample.xyz"
        path.write_text("anything")
        batch = ingest_files([str(path)], vision=vision)
        assert [e.content_text for e in elements_of(batch)] == ["xyz content"]
    finally:
        loader_base._REGISTRY.pop(".xyz", None)


@pytest.mark.parametrize("key", ["pdf", "docx", "pptx", "xlsx", "csv", "txt", "md", "png"])
def test_ac3_every_element_matches_the_schema(samples, vision, key):
    elements = elements_of(ingest_files([samples[key]], vision=vision))
    assert elements
    for element in elements:
        assert validate_element(element) == [], (element.element_id, validate_element(element))
        assert element.metadata["filename"] == os.path.basename(samples[key])


def test_ac4_pdf_text_table_and_image(samples, vision):
    elements = elements_of(ingest_files([samples["pdf"]], vision=vision))
    texts = [e for e in elements if e.type == "text"]
    tables = [e for e in elements if e.type == "table"]
    images = [e for e in elements if e.type in ("image", "chart")]
    assert {e.page for e in texts} == {1, 2}
    assert texts[0].heading_path == ["Introduction"] and texts[-1].heading_path == ["Results"]
    assert "Revenue grew strongly" in texts[0].content_text
    assert "Region" not in texts[0].content_text  # table text is not duplicated in prose
    assert tables and "North" in tables[0].content_text and tables[0].metadata["page"] == 1
    assert images and images[0].content_text == FAKE_DESCRIPTION
    assert "Figure 1" in images[0].metadata["surrounding_text"]


def test_ac5_docx_headings_and_tables(samples, vision):
    elements = elements_of(ingest_files([samples["docx"]], vision=vision))
    pricing = [e for e in elements if e.type == "text" and "ten dollars" in e.content_text][0]
    assert pricing.heading_path == ["Overview", "Pricing"]
    table = [e for e in elements if e.type == "table"][0]
    assert table.content_structured["header"] == ["Plan", "Price"]
    assert table.heading_path == ["Overview", "Pricing"]


def test_ac6a_pptx_slides_charts_pictures_notes(samples, vision):
    elements = elements_of(ingest_files([samples["pptx"]], vision=vision))
    by_slide = lambda n: [e for e in elements if e.metadata["slide"] == n]
    assert any("HydraRAG Launch" in e.content_text for e in by_slide(1))
    assert any("Ship ingestion" in e.content_text for e in by_slide(2))
    notes = [e for e in elements if e.metadata.get("notes")]
    assert len(notes) == 1 and notes[0].metadata["slide"] == 2 and "WhatsApp" in notes[0].content_text
    assert notes[0].metadata["location"] == "slide 2 speaker notes"
    assert any(e.type == "table" and "Servers" in e.content_text for e in by_slide(3))
    chart = [e for e in by_slide(4) if e.type == "chart"][0]
    assert chart.content_structured["header"] == ["Series", "Q1", "Q2"]
    assert chart.content_structured["rows"][0] == ["Users", 100.0, 250.0]
    picture = [e for e in by_slide(5) if e.type == "image"][0]
    assert picture.content_text == FAKE_DESCRIPTION and picture.title == "Architecture"
    assert all(e.metadata.get("hidden") for e in by_slide(6))
    assert all(e.title for e in elements)


def test_ac6a_legacy_ppt_is_unsupported_with_hint(samples, vision):
    result = ingest_files([samples["ppt"]], vision=vision).files[0]
    assert result.status == "unsupported" and "save as .pptx" in result.reason


def test_ac6_workbook_sheets_and_row_ranges(samples, vision):
    elements = elements_of(ingest_files([samples["xlsx"]], vision=vision))
    sales = [e for e in elements if e.metadata["sheet"] == "Sales"][0]
    assert (sales.metadata["row_start"], sales.metadata["row_end"]) == (2, 4)
    assert sales.content_structured["rows"] == [["North", 10, 12], ["South", 7, 9]]  # empty row skipped
    staff = [e for e in elements if e.metadata["sheet"] == "Staff"][0]
    assert staff.content_structured["rows"] == [["Core", "Ana"], ["Core", "Bo"]]  # merged cell repeats


def test_large_sheet_is_capped_and_batched(tmp_path, vision):
    path = tmp_path / "big.csv"
    path.write_text("n,square\n" + "".join(f"{i},{i * i}\n" for i in range(1, 121)))
    settings = IngestionSettings(rows_per_element=50, max_rows_per_sheet=100)
    batch = ingest_files([str(path)], vision=vision, settings=settings)
    elements = elements_of(batch)
    assert [(e.metadata["row_start"], e.metadata["row_end"]) for e in elements] == [(2, 51), (52, 101)]
    assert all(e.content_text.startswith("| n | square |") for e in elements)
    assert any("20 row(s) skipped" in w for w in batch.files[0].warnings)


def test_ac8_duplicates_and_versions(samples, vision, tmp_path):
    first = ingest_files([samples["txt"]], vision=vision)
    again = ingest_files([samples["txt"]], first.session_id, vision=vision).files[0]
    assert again.duplicate and again.doc_id == first.files[0].doc_id
    assert len(SessionStore(first.session_id).documents()) == 1

    changed = tmp_path / "other" / "notes.txt"
    changed.parent.mkdir()
    changed.write_text("Different content now.\n")
    newer = ingest_files([str(changed)], first.session_id, vision=vision).files[0]
    assert not newer.duplicate and newer.version_id == "v2" and first.files[0].version_id == "v1"


def test_ac9_same_name_files_are_both_kept(samples, vision, tmp_path):
    other = tmp_path / "copy" / "data.csv"
    other.parent.mkdir()
    other.write_text("city,population\nQuetta,1000000\n")
    batch = ingest_files([samples["csv"], str(other)], vision=vision)
    stored = [f.stored_path for f in batch.files]
    assert len(set(stored)) == 2 and all(os.path.exists(p) for p in stored)
    assert os.path.basename(stored[1]) == "data_1.csv"


def test_ac10_bad_files_do_not_stop_the_batch(samples, vision):
    batch = ingest_files([samples["csv"], samples["corrupt_pdf"], samples["exe"]], vision=vision)
    statuses = [(f.status, f.reason) for f in batch.files]
    assert statuses[0][0] == "ok" and batch.files[0].element_count > 0
    assert statuses[1][0] == "failed" and "corrupt" in statuses[1][1]
    assert statuses[2][0] == "unsupported" and ".pdf" in statuses[2][1]
    assert batch.error is None
    store = SessionStore(batch.session_id)
    assert store.read_elements(batch.files[1].doc_id) == []  # nothing partial stored for the failed file


@pytest.mark.parametrize(
    "key,reason",
    [
        ("locked_pdf", "password protected"),
        ("locked_docx", "password protected"),
        ("empty_txt", "no readable content"),
    ],
)
def test_unreadable_files_fail_with_a_reason(samples, vision, key, reason):
    result = ingest_files([samples[key]], vision=vision).files[0]
    assert result.status == "failed" and reason in result.reason


def test_ac11_limits_reject_before_processing(samples, vision, data_root):
    tiny = IngestionSettings(max_file_mb=0)
    result = ingest_files([samples["csv"]], vision=vision, settings=tiny)
    assert result.files[0].status == "failed" and "too large" in result.files[0].reason
    assert not os.path.exists(os.path.join(data_root, result.session_id, "spreadsheets"))

    few = IngestionSettings(max_files_per_upload=2)
    batch = ingest_files([samples["csv"], samples["txt"], samples["md"]], vision=vision, settings=few)
    assert "too many files" in batch.error and all(f.status == "failed" for f in batch.files)
    assert not os.path.exists(os.path.join(data_root, batch.session_id))


def test_ac12_vision_retries_then_succeeds(samples):
    calls = {"n": 0}

    def flaky(prompt, image_url):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("429 rate limited")
        return "This image contains a recovered description."

    vision = VisionDescriber(invoke=flaky, wait_min=0, wait_max=0, max_concurrency=1)
    elements = elements_of(ingest_files([samples["png"]], vision=vision))
    assert elements[0].content_text == "This image contains a recovered description."
    assert calls["n"] == 4


def test_ac12_vision_always_failing_flags_image_and_finishes(samples):
    def down(prompt, image_url):
        raise RuntimeError("503 service unavailable")

    vision = VisionDescriber(invoke=down, max_attempts=2, wait_min=0, wait_max=0)
    batch = ingest_files([samples["pdf"]], vision=vision)
    assert batch.files[0].status == "ok"
    image = [e for e in elements_of(batch) if e.type == "image"][0]
    assert image.metadata["description_failed"] and image.content_text == ""
    assert any("image description" in w for w in batch.files[0].warnings)

    lone = ingest_files([samples["png"]], vision=vision).files[0]
    assert lone.status == "failed" and "image description failed" in lone.reason


def test_scanned_pdf_pages_go_through_vision(samples, vision):
    elements = elements_of(ingest_files([samples["scanned_pdf"]], vision=vision))
    assert len(elements) == 1 and elements[0].metadata["scanned_page"] and elements[0].page == 1


def test_image_cap_is_reported(samples, vision):
    settings = IngestionSettings(max_images_per_doc=0)
    result = ingest_files([samples["pptx"]], vision=vision, settings=settings).files[0]
    assert result.status == "ok" and any("not described" in w for w in result.warnings)


def test_ac13_invalid_session_id_writes_nothing(samples, vision, data_root):
    with pytest.raises(ValueError):
        ingest_files([samples["csv"]], "../x", vision=vision)
    assert not os.path.exists(data_root) or os.listdir(data_root) == []


def test_document_states_are_recorded(samples, vision):
    batch = ingest_files([samples["docx"], samples["corrupt_pdf"]], vision=vision)
    docs = SessionStore(batch.session_id).documents()
    states = {d["filename"]: d["state"] for d in docs.values()}
    assert states == {"guide.docx": READY, "broken.pdf": "failed"}


def test_failed_document_can_be_retried(samples, vision, tmp_path):
    def down(prompt, image_url):
        raise RuntimeError("down")

    first = ingest_files([samples["png"]], vision=VisionDescriber(invoke=down, max_attempts=1)).files[0]
    assert first.status == "failed"
    retry = ingest_files([samples["png"]], vision=vision).files[0]
    assert retry.status == "ok" and not retry.duplicate and retry.doc_id == first.doc_id


def test_disk_error_aborts_the_batch(samples, vision, monkeypatch):
    import src.pipeline.ingest as ingest_module

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ingest_module, "store_file", full_disk)
    batch = ingest_files([samples["csv"], samples["txt"]], vision=vision)
    assert "could not store file" in batch.error
    assert [f.status for f in batch.files] == ["failed", "failed"]
