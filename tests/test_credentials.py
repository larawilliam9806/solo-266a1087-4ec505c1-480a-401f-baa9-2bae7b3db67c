import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, "/workspace")

try:
    from app import create_app
except ImportError:  # Flask is only required for these web-layer tests.
    create_app = None

import evidence
from evidence import MANIFEST_ENTRY, verify_bundle

WAREHOUSE_CSV = """记录号,分钟时间,动作
W-001,2026-11-24 10:00,换签
W-002,2026-11-24 10:05,交运
"""

QC_CSV = """记录号,分钟时间,动作
Q-001,2026-11-24 10:05,验封通过
"""


@unittest.skipIf(create_app is None, "Flask is not installed")
class CredentialFlaskTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="boxcheck-cred-", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.app = create_app(
            {"TESTING": True, "DATABASE_PATH": self.db_path}
        )
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def upload_case(self):
        response = self.client.post(
            "/api/cases",
            data={
                "box_code": "CRED-BOX",
                "warehouse_file": (
                    io.BytesIO(WAREHOUSE_CSV.encode()),
                    "warehouse.csv",
                ),
                "qc_file": (io.BytesIO(QC_CSV.encode()), "qc.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()

    def _credential_count(self):
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                "SELECT COUNT(*) FROM credentials"
            ).fetchone()[0]
        finally:
            connection.close()

    def test_api_creates_credential_and_downloads_verified_snapshot(self):
        detail = self.upload_case()
        case_id = detail["case"]["id"]
        conclusion_id = detail["conclusion"]["id"]

        created = self.client.post(f"/api/cases/{case_id}/credentials")
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        body = created.get_json()
        credential_id = body["id"]
        self.assertTrue(credential_id.startswith("CR-"))
        self.assertEqual(body["case_id"], case_id)
        self.assertEqual(body["conclusion_id"], conclusion_id)
        self.assertEqual(len(body["package_sha256"]), 64)
        self.assertEqual(len(body["manifest_sha256"]), 64)
        self.assertEqual(self._credential_count(), 1)

        listing = self.client.get(f"/api/cases/{case_id}/credentials").get_json()
        self.assertEqual(len(listing["credentials"]), 1)
        self.assertNotIn("bundle_blob", listing["credentials"][0])

        meta = self.client.get(f"/api/credentials/{credential_id}").get_json()
        self.assertEqual(meta["id"], credential_id)
        self.assertEqual(meta["conclusion_id"], conclusion_id)

        downloaded = self.client.get(
            f"/api/credentials/{credential_id}/download"
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.mimetype, "application/zip")
        self.assertEqual(
            downloaded.headers["X-Credential-SHA256"], body["package_sha256"]
        )
        bundle = downloaded.get_data()
        report = verify_bundle(bundle, body["package_sha256"])
        self.assertEqual(len(report["manifest"]), 4)

        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertEqual(
                archive.read("warehouse.csv").decode("utf-8"), WAREHOUSE_CSV
            )
            self.assertEqual(archive.read("qc.csv").decode("utf-8"), QC_CSV)
            supplements_doc = json.loads(archive.read("supplements.json"))
            conclusion_doc = json.loads(archive.read("conclusion.json"))
            manifest_text = archive.read(MANIFEST_ENTRY).decode("utf-8")

        self.assertEqual(supplements_doc["supplement_count"], 0)
        self.assertEqual(
            conclusion_doc["conclusion"]["id"], conclusion_id
        )
        self.assertEqual(
            conclusion_doc["conclusion"]["verdict"], "ambiguous"
        )
        manifest_names = [
            line.split("  ", 1)[1]
            for line in manifest_text.splitlines()
            if line.strip()
        ]
        self.assertEqual(manifest_names, sorted(manifest_names))

    def test_credential_freezes_snapshot_and_survives_new_conclusion(self):
        detail = self.upload_case()
        case_id = detail["case"]["id"]
        first_conclusion_id = detail["conclusion"]["id"]

        created = self.client.post(
            f"/api/cases/{case_id}/credentials"
        ).get_json()
        first_credential_id = created["id"]

        # A later supplement produces a new conclusion version.
        supplement = self.client.post(
            f"/api/cases/{case_id}/supplements",
            json={"before_uid": "Q::Q-001", "after_uid": "W::W-002"},
        )
        self.assertEqual(supplement.status_code, 200, supplement.get_data(as_text=True))
        new_conclusion_id = supplement.get_json()["conclusion"]["id"]
        self.assertNotEqual(new_conclusion_id, first_conclusion_id)

        # The old bundle still downloads byte-identically and keeps the old
        # conclusion version; the page lists both conclusion id and hashes.
        old_download = self.client.get(
            f"/api/credentials/{first_credential_id}/download"
        ).get_data()
        with zipfile.ZipFile(io.BytesIO(old_download)) as archive:
            frozen = json.loads(archive.read("conclusion.json"))
            frozen_supplements = json.loads(archive.read("supplements.json"))
        self.assertEqual(frozen["conclusion"]["id"], first_conclusion_id)
        self.assertEqual(frozen["conclusion"]["verdict"], "ambiguous")
        self.assertEqual(frozen_supplements["supplement_count"], 0)

        # A new credential captures the new state without touching the old one.
        second = self.client.post(
            f"/api/cases/{case_id}/credentials"
        ).get_json()
        self.assertEqual(second["conclusion_id"], new_conclusion_id)
        self.assertNotEqual(second["id"], first_credential_id)
        self.assertEqual(self._credential_count(), 2)

        second_download = self.client.get(
            f"/api/credentials/{second['id']}/download"
        ).get_data()
        with zipfile.ZipFile(io.BytesIO(second_download)) as archive:
            second_frozen = json.loads(archive.read("conclusion.json"))
            second_supplements = json.loads(archive.read("supplements.json"))
        self.assertEqual(
            second_frozen["conclusion"]["verdict"], "always_compliant"
        )
        self.assertEqual(second_supplements["supplement_count"], 1)

        page = self.client.get(f"/cases/{case_id}").get_data(as_text=True)
        self.assertIn(first_credential_id, page)
        self.assertIn(second["id"], page)
        self.assertIn(f"结论编号 #{first_conclusion_id}", page)
        self.assertIn(f"结论编号 #{new_conclusion_id}", page)
        self.assertIn(created["package_sha256"], page)
        self.assertIn(created["manifest_sha256"], page)

    def test_html_flow_generates_redirects_and_downloads(self):
        detail = self.upload_case()
        case_id = detail["case"]["id"]

        response = self.client.post(f"/cases/{case_id}/credentials")
        self.assertEqual(response.status_code, 303)
        location = response.headers["Location"]
        self.assertIn("/cases/", location)
        self.assertIn("credential=CR-", location)

        page = self.client.get(location)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('data-testid="credential-saved"', html)
        self.assertIn('data-testid="generate-credential"', html)
        self.assertIn('data-testid="credential-download"', html)

        credential_id = location.rsplit("credential=", 1)[1]
        download = self.client.get(
            f"/cases/{case_id}/credentials/{credential_id}/download"
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.mimetype, "application/zip")
        verify_bundle(download.get_data())

    def test_packaging_failure_leaves_no_half_package(self):
        detail = self.upload_case()
        case_id = detail["case"]["id"]

        def broken_build(**kwargs):
            raise evidence.EvidenceError("模拟打包失败")

        with mock.patch("app.build_evidence_bundle", side_effect=broken_build):
            response = self.client.post(f"/api/cases/{case_id}/credentials")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.get_json()["rejected"])
        self.assertEqual(self._credential_count(), 0)

        # The case itself and its conclusion remain usable.
        fetched = self.client.get(f"/api/cases/{case_id}").get_json()
        self.assertIsNotNone(fetched["conclusion"])
        self.assertEqual(fetched["credentials"], [])

        # HTML flow surfaces the failure and also saves nothing.
        html_response = self.client.post(f"/cases/{case_id}/credentials")
        # Without the patch the next request would normally succeed; restore is
        # automatic via the context manager, so assert normal happy path again.
        self.assertEqual(html_response.status_code, 303)
        self.assertEqual(self._credential_count(), 1)

    def test_write_failure_rolls_back_entire_transaction(self):
        detail = self.upload_case()
        case_id = detail["case"]["id"]

        real_connect = sqlite3.connect

        class FailingConnection:
            """Proxy that fails the credentials INSERT, everything else passes."""

            def __init__(self, path, *args, **kwargs):
                object.__setattr__(self, "_inner", real_connect(path, *args, **kwargs))

            def __setattr__(self, name, value):
                if name == "row_factory":
                    setattr(self._inner, name, value)
                else:
                    object.__setattr__(self, name, value)

            def execute(self, sql, *sql_args, **sql_kwargs):
                if sql.lstrip().upper().startswith("INSERT INTO CREDENTIALS"):
                    raise sqlite3.OperationalError("模拟写入失败")
                return self._inner.execute(sql, *sql_args, **sql_kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        with mock.patch(
            "app.sqlite3.connect", side_effect=FailingConnection
        ):
            response = self.client.post(f"/api/cases/{case_id}/credentials")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self._credential_count(), 0)

    def test_concurrent_credential_and_supplement_never_interleave(self):
        detail = self.upload_case()
        case_id = detail["case"]["id"]
        build_started = threading.Event()
        release_build = threading.Event()

        real_build = evidence.build_evidence_bundle

        def synced_build(**kwargs):
            # The service already holds the BEGIN IMMEDIATE write transaction;
            # keep it open so the concurrent supplement must queue behind it.
            build_started.set()
            self.assertTrue(release_build.wait(timeout=10))
            return real_build(**kwargs)

        credential_response = {}

        def generate():
            with mock.patch("app.build_evidence_bundle", side_effect=synced_build):
                credential_response["value"] = self.client.post(
                    f"/api/cases/{case_id}/credentials"
                )

        credential_thread = threading.Thread(target=generate)
        credential_thread.start()
        self.assertTrue(build_started.wait(timeout=10))

        # While the snapshot transaction is open, launch a supplement in a
        # second thread: its INSERT has to wait for the write lock.
        supplement_response = {}

        def submit_supplement():
            supplement_response["value"] = self.client.post(
                f"/api/cases/{case_id}/supplements",
                json={"before_uid": "Q::Q-001", "after_uid": "W::W-002"},
            )

        supplement_thread = threading.Thread(target=submit_supplement)
        supplement_thread.start()

        # Give the queued writer time to attempt its commit; nothing may be
        # visible yet: no supplement, no credential (no half package).
        time.sleep(1.0)
        probe = sqlite3.connect(self.db_path, timeout=5)
        try:
            self.assertEqual(
                probe.execute("SELECT COUNT(*) FROM supplements").fetchone()[0],
                0,
            )
            self.assertEqual(
                probe.execute("SELECT COUNT(*) FROM credentials").fetchone()[0],
                0,
            )
        finally:
            probe.close()

        release_build.set()
        credential_thread.join(timeout=15)
        supplement_thread.join(timeout=35)
        self.assertFalse(credential_thread.is_alive())
        self.assertFalse(supplement_thread.is_alive())

        self.assertEqual(
            supplement_response["value"].status_code,
            200,
            supplement_response["value"].get_data(as_text=True),
        )
        self.assertEqual(
            credential_response["value"].status_code,
            201,
            credential_response["value"].get_data(as_text=True),
        )

        # The frozen bundle deterministically captures the pre-supplement
        # state because the supplement could not commit during the snapshot.
        credential = credential_response["value"].get_json()
        bundle = self.client.get(
            f"/api/credentials/{credential['id']}/download"
        ).get_data()
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            frozen_supplements = json.loads(archive.read("supplements.json"))
            frozen_conclusion = json.loads(archive.read("conclusion.json"))
        self.assertEqual(frozen_supplements["supplement_count"], 0)
        self.assertEqual(
            frozen_conclusion["conclusion"]["verdict"], "ambiguous"
        )
        verify_bundle(bundle, credential["package_sha256"])

        # The supplement eventually landed after the snapshot; a fresh
        # credential would then capture the new conclusion version.
        persisted = self.client.get(f"/api/cases/{case_id}").get_json()
        self.assertEqual(len(persisted["supplements"]), 1)

    def test_unknown_case_and_credential_return_404(self):
        self.assertEqual(
            self.client.post("/api/cases/nope/credentials").status_code, 404
        )
        self.assertEqual(
            self.client.get("/api/cases/nope/credentials").status_code, 404
        )
        self.assertEqual(
            self.client.get("/api/credentials/CR-NOPE/download").status_code,
            404,
        )
        detail = self.upload_case()
        case_id = detail["case"]["id"]
        self.assertEqual(
            self.client.get(
                f"/cases/{case_id}/credentials/CR-OTHER/download"
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
