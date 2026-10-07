import tempfile
import unittest
from pathlib import Path
from unittest import mock

import license_manager


class LicenseReactivationTests(unittest.TestCase):
    def test_same_device_receipt_recovers_legacy_server_binding(self) -> None:
        config = {
            "edition": "advanced",
            "version": "2.0.61 Beta",
            "license": {"api_base_url": "https://license.invalid"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "license-reactivation.json"
            local_license = root / "license.json"
            receipt.write_text(
                '{"license_key":"STT-TEST-TEST-TEST-TEST",'
                '"device_hash":"same-device","edition":"advanced","mode":"server"}',
                encoding="utf-8",
            )
            with (
                mock.patch.object(license_manager, "REACTIVATION_FILE", receipt),
                mock.patch.object(license_manager, "device_hash", return_value="same-device"),
                mock.patch.object(license_manager, "license_file", return_value=local_license),
                mock.patch.object(
                    license_manager,
                    "activate_with_server",
                    return_value=(False, "激活码已绑定其他电脑"),
                ),
            ):
                ok, message = license_manager.activate_license("STT-TEST-TEST-TEST-TEST", config)

            self.assertTrue(ok)
            self.assertEqual(message, "当前电脑已重新激活")
            self.assertTrue(local_license.is_file())
            self.assertFalse(receipt.exists())

    def test_receipt_cannot_activate_a_different_device(self) -> None:
        config = {
            "edition": "advanced",
            "license": {"api_base_url": "https://license.invalid"},
        }
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "license-reactivation.json"
            receipt.write_text(
                '{"license_key":"STT-TEST-TEST-TEST-TEST",'
                '"device_hash":"old-device","edition":"advanced"}',
                encoding="utf-8",
            )
            with (
                mock.patch.object(license_manager, "REACTIVATION_FILE", receipt),
                mock.patch.object(license_manager, "device_hash", return_value="new-device"),
                mock.patch.object(
                    license_manager,
                    "activate_with_server",
                    return_value=(False, "激活码已绑定其他电脑"),
                ),
            ):
                ok, message = license_manager.activate_license("STT-TEST-TEST-TEST-TEST", config)

            self.assertFalse(ok)
            self.assertEqual(message, "激活码已绑定其他电脑")
            self.assertTrue(receipt.exists())


if __name__ == "__main__":
    unittest.main()
