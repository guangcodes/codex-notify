import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_distribution_contents
from scripts import verify_release_integrity


class DistributionContentsTests(unittest.TestCase):
    def test_macos_home_path_without_trailing_slash_is_rejected(self):
        sensitive_home = b"/" + b"Users/example"
        for content in (sensitive_home, b"HOME=" + sensitive_home):
            with self.subTest(content=content):
                with self.assertRaisesRegex(ValueError, "macOS 用户绝对路径"):
                    verify_distribution_contents._verify_entry("fixture.sh", content)

    def test_sensitive_path_in_unlisted_text_suffix_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "codex_notify-0.1.0"
            leak = root / "tests" / "leak.sh"
            leak.parent.mkdir(parents=True)
            sensitive_path = "/" + "Users/example/private/tool\n"
            leak.write_text(sensitive_path, encoding="utf-8")
            archive = Path(directory) / "codex_notify-0.1.0.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                handle.add(root, arcname=root.name)

            with self.assertRaisesRegex(ValueError, "macOS 用户绝对路径"):
                verify_distribution_contents._verify_sdist(archive)


class ReleaseIntegrityTests(unittest.TestCase):
    def _files(self, directory: str) -> dict[str, Path]:
        dist = Path(directory)
        wheel = dist / "codex_notify-0.1.0-py3-none-any.whl"
        sdist = dist / "codex_notify-0.1.0.tar.gz"
        wheel.write_bytes(b"wheel")
        sdist.write_bytes(b"sdist")
        return {wheel.name: wheel, sdist.name: sdist}

    @staticmethod
    def _digests(files: dict[str, Path]) -> dict[str, str]:
        return {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in files.items()
        }

    def test_existing_matching_assets_are_not_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self._files(directory)
            digests = self._digests(files)
            release = {
                "assets": [
                    {"name": name, "digest": f"sha256:{digest}"}
                    for name, digest in digests.items()
                ],
                "upload_url": "https://uploads.example/assets{?name,label}",
            }
            with patch.object(
                verify_release_integrity,
                "_request",
                return_value=json.dumps(release).encode(),
            ) as request:
                result = verify_release_integrity._ensure_github_assets(
                    "owner/repo", "v0.1.0", files, token="token"
                )

            self.assertEqual(result, digests)
            request.assert_called_once()

    def test_only_missing_asset_is_uploaded_without_clobber_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self._files(directory)
            digests = self._digests(files)
            existing_name = next(iter(files))
            missing_name = next(name for name in files if name != existing_name)
            initial = {
                "assets": [
                    {
                        "name": existing_name,
                        "digest": f"sha256:{digests[existing_name]}",
                    }
                ],
                "upload_url": "https://uploads.example/assets{?name,label}",
            }
            complete = {
                "assets": [
                    {"name": name, "digest": f"sha256:{digest}"}
                    for name, digest in digests.items()
                ],
                "upload_url": initial["upload_url"],
            }
            responses = [
                json.dumps(initial).encode(),
                b"{}",
                json.dumps(complete).encode(),
            ]
            with patch.object(
                verify_release_integrity, "_request", side_effect=responses
            ) as request:
                result = verify_release_integrity._ensure_github_assets(
                    "owner/repo", "v0.1.0", files, token="token"
                )

            self.assertEqual(result, digests)
            upload_calls = [
                call
                for call in request.call_args_list
                if call.kwargs.get("data") is not None
            ]
            self.assertEqual(len(upload_calls), 1)
            self.assertIn(f"name={missing_name}", upload_calls[0].args[0])
            self.assertEqual(
                upload_calls[0].kwargs["data"], files[missing_name].read_bytes()
            )
            self.assertTrue(
                all(
                    call.kwargs["content_type"] == "application/octet-stream"
                    for call in upload_calls
                )
            )

    def test_mismatched_existing_asset_fails_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self._files(directory)
            digests = self._digests(files)
            assets = [
                {"name": name, "digest": f"sha256:{digest}"}
                for name, digest in digests.items()
            ]
            assets[0]["digest"] = "sha256:wrong"
            release = {
                "assets": assets,
                "upload_url": "https://uploads.example/assets{?name,label}",
            }
            with patch.object(
                verify_release_integrity,
                "_request",
                return_value=json.dumps(release).encode(),
            ) as request:
                with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
                    verify_release_integrity._ensure_github_assets(
                        "owner/repo", "v0.1.0", files, token="token"
                    )

            request.assert_called_once()

    def test_pypi_hash_check_retries_incomplete_success_response(self):
        expected = {
            "codex_notify-0.1.0-py3-none-any.whl": "wheel-digest",
            "codex_notify-0.1.0.tar.gz": "sdist-digest",
        }
        partial = {
            "urls": [
                {
                    "filename": "codex_notify-0.1.0-py3-none-any.whl",
                    "digests": {"sha256": "wheel-digest"},
                }
            ]
        }
        complete = {
            "urls": [
                {"filename": filename, "digests": {"sha256": digest}}
                for filename, digest in expected.items()
            ]
        }
        with patch.object(
            verify_release_integrity,
            "_request",
            side_effect=[json.dumps(partial).encode(), json.dumps(complete).encode()],
        ) as request, patch.object(verify_release_integrity.time, "sleep") as sleep:
            result = verify_release_integrity._retry_pypi_hashes(
                "https://pypi.example/project.json", expected, attempts=2
            )

        self.assertEqual(result, expected)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
