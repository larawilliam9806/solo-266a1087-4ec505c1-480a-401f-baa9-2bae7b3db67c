"""Immutable evidence-bundle construction for case credentials.

The module is deliberately independent of Flask and SQLite: given a snapshot of
the two original CSV texts, the accepted supplements and the conclusion version
that was current at generation time, it produces a deterministic ZIP bundle and
its SHA-256 digests.

Bundle layout (fixed entry order)::

    warehouse.csv        original warehouse CSV snapshot (UTF-8)
    qc.csv               original QC CSV snapshot (UTF-8)
    supplements.json     accepted supplements at snapshot time
    conclusion.json      the conclusion version frozen at snapshot time
    MANIFEST.sha256      SHA-256 of the four payload files, sorted by filename

ZIP entries carry fixed timestamps and permissions so the same snapshot always
produces byte-identical bundles.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any

WAREHOUSE_ENTRY = "warehouse.csv"
QC_ENTRY = "qc.csv"
SUPPLEMENTS_ENTRY = "supplements.json"
CONCLUSION_ENTRY = "conclusion.json"
MANIFEST_ENTRY = "MANIFEST.sha256"

#: Payload entries covered by the manifest, in the fixed archive order.
PAYLOAD_ENTRIES = (
    WAREHOUSE_ENTRY,
    QC_ENTRY,
    SUPPLEMENTS_ENTRY,
    CONCLUSION_ENTRY,
)

#: Full archive order including the manifest.
ARCHIVE_ENTRIES = PAYLOAD_ENTRIES + (MANIFEST_ENTRY,)

#: Fixed DOS timestamp / permissions keep archives reproducible.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_ZIP_EXTERNAL_ATTR = 0o644 << 16


class EvidenceError(Exception):
    """Raised when a bundle cannot be built or fails verification."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_csv_text(text: str) -> bytes:
    """Re-emit the decoded SQLite CSV snapshot as stable UTF-8 bytes."""
    return text.encode("utf-8")


def _json_bytes(document: Any) -> bytes:
    # sort_keys + fixed indent make the serialisation deterministic.
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def build_manifest(entries: list[tuple[str, bytes]]) -> bytes:
    """Build a ``sha256sum``-style manifest sorted by the filename field."""
    ordered = sorted(entries, key=lambda item: item[0])
    lines = [f"{sha256_hex(data)}  {name}\n" for name, data in ordered]
    return "".join(lines).encode("utf-8")


def build_evidence_bundle(
    *,
    case_id: str,
    box_code: str,
    warehouse_filename: str | None,
    qc_filename: str | None,
    warehouse_csv: str,
    qc_csv: str,
    supplements: list[dict[str, Any]],
    conclusion: dict[str, Any],
    generated_at: str,
) -> tuple[bytes, str, str]:
    """Build the deterministic ZIP bundle.

    Returns ``(bundle_bytes, package_sha256, manifest_text)``.  Everything is
    assembled in memory; any failure raises before bytes are returned, so the
    caller's database transaction can be rolled back without leaving a partial
    package anywhere.
    """
    warehouse_bytes = encode_csv_text(warehouse_csv)
    qc_bytes = encode_csv_text(qc_csv)

    supplements_document = {
        "case_id": case_id,
        "box_code": box_code,
        "generated_at": generated_at,
        "source_files": {
            "warehouse": warehouse_filename,
            "qc": qc_filename,
        },
        "supplement_count": len(supplements),
        "supplements": supplements,
    }
    conclusion_document = {
        "case_id": case_id,
        "box_code": box_code,
        "generated_at": generated_at,
        "conclusion": conclusion,
    }

    payload_entries: list[tuple[str, bytes]] = [
        (WAREHOUSE_ENTRY, warehouse_bytes),
        (QC_ENTRY, qc_bytes),
        (SUPPLEMENTS_ENTRY, _json_bytes(supplements_document)),
        (CONCLUSION_ENTRY, _json_bytes(conclusion_document)),
    ]
    manifest_bytes = build_manifest(payload_entries)

    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, data in [*payload_entries, (MANIFEST_ENTRY, manifest_bytes)]:
                info = zipfile.ZipInfo(filename=name, date_time=_ZIP_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = _ZIP_EXTERNAL_ATTR
                info.create_system = 3
                archive.writestr(info, data)
    except (OSError, zipfile.BadZipFile, UnicodeError) as exc:
        raise EvidenceError(f"凭据打包失败：{exc}") from exc

    bundle = buffer.getvalue()
    return bundle, sha256_hex(bundle), manifest_bytes.decode("utf-8")


def verify_bundle(bundle: bytes, expected_package_sha256: str | None = None) -> dict[str, Any]:
    """Re-open a bundle and verify every manifest digest.

    Raises :class:`EvidenceError` on any mismatch.  Returns a report with the
    parsed manifest and the package-level SHA-256 when verification succeeds.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle))
    except zipfile.BadZipFile as exc:
        raise EvidenceError("凭据不是有效的 ZIP 文件") from exc

    with archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise EvidenceError(f"凭据 ZIP 中存在损坏条目：{corrupt}")
        names = archive.namelist()
        if names != list(ARCHIVE_ENTRIES):
            raise EvidenceError(f"凭据条目不符合固定清单：{names}")
        payload = {
            name: archive.read(name)
            for name in names
            if name != MANIFEST_ENTRY
        }
        manifest_text = archive.read(MANIFEST_ENTRY).decode("utf-8")

    parsed_manifest: dict[str, str] = {}
    for line_no, line in enumerate(manifest_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            digest, name = line.split("  ", 1)
        except ValueError as exc:
            raise EvidenceError(f"清单第 {line_no} 行格式错误") from exc
        parsed_manifest[name] = digest

    manifest_names = list(parsed_manifest)
    if manifest_names != sorted(manifest_names):
        raise EvidenceError("清单未按文件名字段固定排序")
    if set(parsed_manifest) != set(PAYLOAD_ENTRIES):
        raise EvidenceError("清单覆盖的文件与凭据固定条目不一致")

    for name, data in payload.items():
        actual = sha256_hex(data)
        if parsed_manifest.get(name) != actual:
            raise EvidenceError(f"文件 {name} 的 SHA-256 与清单不符")

    package_sha256 = sha256_hex(bundle)
    if expected_package_sha256 is not None and package_sha256 != expected_package_sha256:
        raise EvidenceError("凭据整包 SHA-256 与登记摘要不符")

    return {
        "package_sha256": package_sha256,
        "manifest": parsed_manifest,
        "entries": names,
    }
