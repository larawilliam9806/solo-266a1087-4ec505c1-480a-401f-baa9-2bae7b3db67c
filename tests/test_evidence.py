import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, "/workspace")

from evidence import (
    ARCHIVE_ENTRIES,
    CONCLUSION_ENTRY,
    MANIFEST_ENTRY,
    QC_ENTRY,
    SUPPLEMENTS_ENTRY,
    WAREHOUSE_ENTRY,
    EvidenceError,
    build_evidence_bundle,
    sha256_hex,
    verify_bundle,
)

WAREHOUSE_CSV = "记录号,分钟时间,动作\nW-001,2026-11-24 10:00,换签\nW-002,2026-11-24 10:05,交运\n"
QC_CSV = "记录号,分钟时间,动作\nQ-001,2026-11-24 10:05,验封通过\n"

SUPPLEMENTS = [
    {
        "id": 1,
        "case_id": "case-1",
        "before_uid": "Q::Q-001",
        "after_uid": "W::W-002",
        "before_table": "qc",
        "before_record_no": "Q-001",
        "after_table": "warehouse",
        "after_record_no": "W-002",
        "created_at": "2026-11-24T10:10:00+00:00",
    }
]

CONCLUSION = {
    "id": 7,
    "verdict": "always_compliant",
    "verdict_text": "必然合格",
    "compliant_possible": True,
    "noncompliant_possible": False,
    "compliant_order": ["W::W-001", "Q::Q-001", "W::W-002"],
    "noncompliant_order": None,
    "record_count": 3,
    "supplement_count": 1,
}


def _build():
    return build_evidence_bundle(
        case_id="case-1",
        box_code="BOX-017",
        warehouse_filename="warehouse.csv",
        qc_filename="qc.csv",
        warehouse_csv=WAREHOUSE_CSV,
        qc_csv=QC_CSV,
        supplements=SUPPLEMENTS,
        conclusion=CONCLUSION,
        generated_at="2026-11-24T10:12:00+00:00",
    )


class EvidenceBundleTest(unittest.TestCase):
    def test_bundle_layout_and_original_bytes(self):
        bundle, package_sha256, manifest_text = _build()
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertEqual(archive.namelist(), list(ARCHIVE_ENTRIES))
            self.assertEqual(
                archive.read(WAREHOUSE_ENTRY), WAREHOUSE_CSV.encode("utf-8")
            )
            self.assertEqual(archive.read(QC_ENTRY), QC_CSV.encode("utf-8"))
            supplements_text = archive.read(SUPPLEMENTS_ENTRY).decode("utf-8")
            conclusion_text = archive.read(CONCLUSION_ENTRY).decode("utf-8")
            stored_manifest = archive.read(MANIFEST_ENTRY).decode("utf-8")
            for info in archive.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))

        self.assertIn("Q::Q-001", supplements_text)
        self.assertIn("always_compliant", conclusion_text)
        self.assertIn('"id": 7', conclusion_text)
        self.assertEqual(stored_manifest, manifest_text)
        self.assertEqual(sha256_hex(bundle), package_sha256)

    def test_manifest_sorted_by_filename_with_sha256_lines(self):
        _bundle, _digest, manifest_text = _build()
        lines = [line for line in manifest_text.splitlines() if line.strip()]
        names = [line.split("  ", 1)[1] for line in lines]
        self.assertEqual(names, sorted(names))
        self.assertEqual(
            names,
            sorted(
                [WAREHOUSE_ENTRY, QC_ENTRY, SUPPLEMENTS_ENTRY, CONCLUSION_ENTRY]
            ),
        )
        for line in lines:
            digest, name = line.split("  ", 1)
            self.assertEqual(len(digest), 64)
            self.assertTrue(
                all(c in "0123456789abcdef" for c in digest),
                "digest must be lowercase hex",
            )

    def test_verify_accepts_good_bundle_and_detects_tampering(self):
        bundle, package_sha256, _manifest = _build()
        report = verify_bundle(bundle, package_sha256)
        self.assertEqual(report["package_sha256"], package_sha256)
        self.assertEqual(
            set(report["manifest"]),
            {WAREHOUSE_ENTRY, QC_ENTRY, SUPPLEMENTS_ENTRY, CONCLUSION_ENTRY},
        )

        # Tamper with a payload byte: verification must fail.
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries[QC_ENTRY] = entries[QC_ENTRY].replace(b"Q-001", b"Q-999")
        tampered = io.BytesIO()
        with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ARCHIVE_ENTRIES:
                archive.writestr(name, entries[name])
        with self.assertRaises(EvidenceError):
            verify_bundle(tampered.getvalue())

        with self.assertRaises(EvidenceError):
            verify_bundle(bundle, "0" * 64)

    def test_deterministic_build_is_byte_identical(self):
        first, first_digest, _ = _build()
        second, second_digest, _ = _build()
        self.assertEqual(first, second)
        self.assertEqual(first_digest, second_digest)

    def test_different_snapshot_changes_digest(self):
        first, first_digest, _ = _build()
        changed_conclusion = dict(CONCLUSION, verdict="ambiguous", id=8)
        second, second_digest, _ = build_evidence_bundle(
            case_id="case-1",
            box_code="BOX-017",
            warehouse_filename="warehouse.csv",
            qc_filename="qc.csv",
            warehouse_csv=WAREHOUSE_CSV,
            qc_csv=QC_CSV,
            supplements=SUPPLEMENTS,
            conclusion=changed_conclusion,
            generated_at="2026-11-24T10:20:00+00:00",
        )
        self.assertNotEqual(first, second)
        self.assertNotEqual(first_digest, second_digest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
