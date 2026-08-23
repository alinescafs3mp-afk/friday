"""Synthetic, network-free coverage for the optional local scan fallback."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from friday.config import PROFILES
from friday.documents import DocumentExtractor, LocalOcrResult, VisualAsset
from friday.documents import _ocr as ocr_module
from friday.documents._ocr import extract_local_ocr
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph


@pytest.fixture(autouse=True)
def _clean_operator_ocr_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FRIDAY_TESSERACT_PATH",
        "JERICHO_TESSERACT_PATH",
        "FRIDAY_TESSDATA_DIR",
        "JERICHO_TESSDATA_DIR",
        "FRIDAY_TESSERACT_LANGUAGES",
        "JERICHO_TESSERACT_LANGUAGES",
        "FRIDAY_TESSERACT_LIBRARY_PATH",
        "JERICHO_TESSERACT_LIBRARY_PATH",
        "TESSDATA_PREFIX",
        "LD_LIBRARY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _fake_tesseract(
    path: Path,
    *,
    expected_library_path: str | None = None,
    languages: tuple[str, ...] = ("rus", "eng"),
    ocr_action: str = "sys.stdout.buffer.write('СИНТЕТИЧЕСКИЙ OCR'.encode('utf-8'))",
) -> Path:
    listed = "\n".join(("List of available languages:", *languages, ""))
    path.write_text(
        f"""#!{sys.executable}
import os
import sys
import time
if os.environ.get('LD_LIBRARY_PATH') != {expected_library_path!r}:
    raise SystemExit(3)
if '--list-langs' in sys.argv[1:]:
    sys.stdout.write({listed!r})
    raise SystemExit(0)
payload = sys.stdin.buffer.read()
if not payload or sys.argv[1:3] != ['stdin', 'stdout']:
    raise SystemExit(2)
{ocr_action}
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _write_tessdata(path: Path, *languages: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for language in languages:
        (path / f"{language}.traineddata").write_bytes(b"synthetic-traineddata")
    return path


def _asset(data: bytes = b"synthetic-normalized-image") -> VisualAsset:
    return VisualAsset(
        data=data,
        mime_type="image/jpeg",
        source="synthetic-page",
        width=640,
        height=480,
    )


def test_local_ocr_uses_a_fixed_absolute_executable_and_bounded_pipe(tmp_path: Path) -> None:
    executable = _fake_tesseract(tmp_path / "tesseract")

    result = DocumentExtractor(secret_values=()).ocr_visual_assets(
        [_asset()],
        executable=str(executable),
    )

    assert result.success is True
    assert result.page_texts == ("СИНТЕТИЧЕСКИЙ OCR",)
    assert result.pages_read == result.pages_total == 1
    assert result.error == ""


def test_local_ocr_canonicalizes_an_operator_executable_symlink(tmp_path: Path) -> None:
    executable = _fake_tesseract(tmp_path / "real-tesseract")
    configured = tmp_path / "tesseract"
    configured.symlink_to(executable)

    assert ocr_module._resolved_executable(str(configured)) == str(executable.resolve())  # noqa: SLF001


def test_local_ocr_discovers_rootless_operator_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "private-runtime" / "bin" / "tesseract"
    tessdata = tmp_path / "private-runtime" / "share" / "tessdata"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable = _fake_tesseract(executable)
    _write_tessdata(tessdata, "rus", "eng")
    monkeypatch.setenv("FRIDAY_TESSERACT_PATH", str(executable))
    monkeypatch.setenv("FRIDAY_TESSDATA_DIR", str(tessdata))
    monkeypatch.setenv("FRIDAY_TESSERACT_LANGUAGES", "rus+eng")

    extractor = DocumentExtractor(secret_values=())
    assert extractor.local_ocr_available() is True
    assert extractor.ocr_visual_assets([_asset()]).success is True


def test_rootless_loader_path_is_validated_and_forwarded_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_dir = tmp_path / "private-runtime" / "lib"
    library_dir.mkdir(parents=True)
    executable = tmp_path / "private-runtime" / "bin" / "tesseract"
    executable.parent.mkdir(parents=True)
    executable = _fake_tesseract(
        executable,
        expected_library_path=str(library_dir.resolve()),
    )
    monkeypatch.setenv("FRIDAY_TESSERACT_PATH", str(executable))
    monkeypatch.setenv("FRIDAY_TESSERACT_LIBRARY_PATH", str(library_dir))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/ambient/value/must/not/leak")

    result = DocumentExtractor(secret_values=()).ocr_visual_assets([_asset()])

    assert result.success is True
    assert result.page_texts == ("СИНТЕТИЧЕСКИЙ OCR",)


def test_explicit_tessdata_requires_every_requested_language_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fake_tesseract(tmp_path / "tesseract")
    tessdata = _write_tessdata(tmp_path / "tessdata", "eng")
    monkeypatch.setenv("FRIDAY_TESSERACT_PATH", str(executable))
    monkeypatch.setenv("FRIDAY_TESSDATA_DIR", str(tessdata))
    monkeypatch.setenv("FRIDAY_TESSERACT_LANGUAGES", "rus+eng")

    extractor = DocumentExtractor(secret_values=())
    result = extractor.ocr_visual_assets([_asset()])

    assert extractor.local_ocr_available() is False
    assert result.success is False
    assert result.error == "local_ocr_configuration_invalid"


def test_compiled_in_tessdata_is_verified_by_the_exact_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fake_tesseract(tmp_path / "tesseract", languages=("eng",))
    monkeypatch.setenv("FRIDAY_TESSERACT_PATH", str(executable))
    monkeypatch.setenv("FRIDAY_TESSERACT_LANGUAGES", "rus+eng")

    extractor = DocumentExtractor(secret_values=())
    result = extractor.ocr_visual_assets([_asset()])

    assert extractor.local_ocr_available() is False
    assert result.success is False
    assert result.error == "local_ocr_configuration_invalid"


class _CarrierlessVision:
    enabled = True
    model = "offline-carrierless-vision"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, str]:
        del kwargs
        self.calls += 1
        prompt = str(messages[-1]["content"][0].get("text") or "")
        if "TARGETED OCR REREAD" in prompt:
            return {"content": json.dumps({"asset_id": "A1", "text": ""})}
        return {
            "content": json.dumps(
                {
                    "pages": [{"asset_id": "A1", "text": ""}],
                    "text": "",
                    "title": "",
                    "summary": "",
                    "document_type": "scan",
                    "confidence": 0.0,
                    "entities": [],
                    "evidence": [],
                    "warnings": [],
                }
            )
        }


@pytest.mark.asyncio
async def test_local_ocr_binary_probe_never_blocks_the_async_request_thread(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
        None,
    )
    request_thread = threading.current_thread()

    def probe() -> bool:
        assert threading.current_thread() is not request_thread
        return False

    monkeypatch.setattr(pipeline._doc_extractor, "local_ocr_available", probe)  # noqa: SLF001

    result = await pipeline._extract_visual_document(  # noqa: SLF001
        b"not-an-image",
        filename="synthetic-scan.jpg",
        mime_type="image/jpeg",
    )

    assert result is None


@pytest.mark.asyncio
async def test_failed_vision_uses_local_ocr_as_advisory_source(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = Image.new("RGB", (640, 480), "white")
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG")
    llm = _CarrierlessVision()
    pipeline = IngestionPipeline(
        replace(settings, profile=PROFILES["qwen36-vl"]),
        storage,
        KnowledgeGraph(storage),
        llm,
    )
    monkeypatch.setattr(pipeline._doc_extractor, "local_ocr_available", lambda: True)  # noqa: SLF001
    monkeypatch.setattr(
        pipeline._doc_extractor,  # noqa: SLF001
        "ocr_visual_assets",
        lambda assets, *, deadline=None: LocalOcrResult(
            ("LOCAL FALLBACK OCR",),
            len(assets),
            1,
        ),
    )

    result = await pipeline._extract_visual_document(  # noqa: SLF001
        encoded.getvalue(),
        filename="synthetic-scan.jpg",
        mime_type="image/jpeg",
    )

    assert result is not None and result["success"] is True
    assert "LOCAL FALLBACK OCR" in result["text"]
    assert result["method"] == "local_tesseract_ocr"
    assert result["model"] == "local-tesseract"
    assert result["advisory_only"] is True
    assert result["pages_read"] == result["pages_total"] == 1
    assert "local_ocr_fallback" in result["warnings"]
    assert llm.calls == 2


def test_non_absolute_operator_executable_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRIDAY_TESSERACT_PATH", "relative/tesseract")
    monkeypatch.setenv("PATH", "")
    assert DocumentExtractor(secret_values=()).local_ocr_available() is False


@pytest.mark.parametrize(
    "configured",
    ["relative/lib", "/absolute/missing/lib", "/tmp:/another/path:"],
)
def test_invalid_rootless_loader_path_is_rejected(
    configured: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fake_tesseract(tmp_path / "tesseract")
    monkeypatch.setenv("FRIDAY_TESSERACT_PATH", str(executable))
    monkeypatch.setenv("FRIDAY_TESSERACT_LIBRARY_PATH", configured)

    extractor = DocumentExtractor(secret_values=())
    result = extractor.ocr_visual_assets([_asset()])

    assert extractor.local_ocr_available() is False
    assert result.error == "local_ocr_configuration_invalid"


def test_local_ocr_timeout_is_a_failed_partial_result(tmp_path: Path) -> None:
    executable = _fake_tesseract(
        tmp_path / "tesseract",
        ocr_action="time.sleep(2)",
    )

    result = extract_local_ocr(
        [_asset()],
        max_text_chars=100_000,
        executable=str(executable),
        deadline=time.monotonic() + 0.1,
    )

    assert result.success is False
    assert result.pages_total == 1
    assert result.pages_read == 0
    assert result.deadline_reached is True
    assert result.error == "local_ocr_deadline_reached"


def test_local_ocr_rejects_a_child_output_over_its_byte_cap(tmp_path: Path) -> None:
    executable = _fake_tesseract(
        tmp_path / "tesseract",
        ocr_action="sys.stdout.buffer.write(b'x' * 1_000_001)",
    )

    result = extract_local_ocr(
        [_asset()],
        max_text_chars=2_000_000,
        executable=str(executable),
    )

    assert result.success is False
    assert result.pages_read == 0
    assert result.error == "local_ocr_failed"


def test_local_ocr_multipage_work_shares_one_deadline(tmp_path: Path) -> None:
    executable = _fake_tesseract(
        tmp_path / "tesseract",
        ocr_action=(
            "sys.stdout.buffer.write(b'FIRST') if payload.startswith(b'first') "
            "else (time.sleep(2), sys.stdout.buffer.write(b'SECOND'))"
        ),
    )

    result = extract_local_ocr(
        [_asset(b"first-page"), _asset(b"slow-second-page")],
        max_text_chars=100_000,
        executable=str(executable),
        deadline=time.monotonic() + 0.2,
    )

    assert result.success is False
    assert result.page_texts == ("FIRST",)
    assert result.pages_total == 2
    assert result.pages_read == 1
    assert result.deadline_reached is True
    assert result.error == "local_ocr_deadline_reached"


def test_local_ocr_empty_later_page_is_an_honest_partial(tmp_path: Path) -> None:
    executable = _fake_tesseract(
        tmp_path / "tesseract",
        ocr_action=(
            "sys.stdout.buffer.write(b'FIRST') if payload.startswith(b'first') else None"
        ),
    )

    result = extract_local_ocr(
        [_asset(b"first-page"), _asset(b"empty-second-page")],
        max_text_chars=100_000,
        executable=str(executable),
    )

    assert result.success is False
    assert result.page_texts == ("FIRST",)
    assert result.pages_total == 2
    assert result.pages_read == 1
    assert result.error == "local_ocr_page_text_empty"


def test_local_ocr_stops_at_the_40_page_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fake_tesseract(tmp_path / "tesseract")
    calls = 0

    def bounded_process(
        command: list[str],
        payload: bytes,
        *,
        deadline: float,
        output_limit: int,
        environment: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        del deadline, output_limit, environment
        nonlocal calls
        if "--list-langs" in command:
            return 0, b"List of available languages:\nrus\neng\n"
        assert payload
        calls += 1
        return 0, b"PAGE"

    monkeypatch.setattr(ocr_module, "_bounded_process", bounded_process)
    ocr_module._LANGUAGE_CACHE.clear()  # noqa: SLF001

    result = extract_local_ocr(
        [_asset(f"page-{index}".encode()) for index in range(45)],
        max_text_chars=100_000,
        executable=str(executable),
    )

    assert calls == 40
    assert result.success is False
    assert result.pages_total == 45
    assert result.pages_read == 40
    assert result.page_cap_reached is True
    assert result.error == "local_ocr_page_cap_reached"
