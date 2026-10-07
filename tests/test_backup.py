import hashlib
import io
import json
import tarfile

import pytest

from ozon_analytics.operations import check_backup


def archive(path, data, expected):
    with tarfile.open(path, "w:gz") as tar:
        for name, content in [
            ("database.dump", data),
            ("manifest.json", json.dumps({"database_sha256": expected}).encode()),
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))


def test_backup_checksum_detects_corruption(tmp_path):
    path = tmp_path / "backup.tar.gz"
    digest = hashlib.sha256(b"valid dump").hexdigest()
    archive(path, b"valid dump", digest)
    assert check_backup(path)["database_sha256"] == digest
    archive(path, b"changed dump", digest)
    with pytest.raises(ValueError, match="checksum mismatch"):
        check_backup(path)
