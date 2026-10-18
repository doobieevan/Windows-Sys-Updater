import unittest
from unittest.mock import patch

from updater import UpdateItem, _process_name_candidates, _version_key, check_for_updates, install_update, install_updates, winget_updates


class UpdaterTests(unittest.TestCase):
    def test_update_comparison(self):
        self.assertTrue(UpdateItem("Tool", "1.2.0", "1.3.0").update_available)
        self.assertFalse(UpdateItem("Tool", "2.0", "1.9").update_available)

    def test_unknown_available_version_is_not_update(self):
        self.assertFalse(UpdateItem("Tool", "1.0", "").update_available)
        self.assertFalse(UpdateItem("Tool", "1.0", "unknown").update_available)

    def test_version_key_handles_build_numbers(self):
        self.assertEqual(_version_key("v2.10-build7"), (2, 10, 7))

    def test_update_item_keeps_package_id(self):
        item = UpdateItem("Game", "1.0", "2.0", package_id="Publisher.Game", source="winget", kind="game")
        self.assertTrue(item.update_available)
        self.assertEqual(item.package_id, "Publisher.Game")

    def test_process_candidates_include_app_but_not_protected_filtering_is_external(self):
        item = UpdateItem("Example App", "1.0", "2.0", package_id="Publisher.ExampleApp", executable="ExampleApp.exe")
        self.assertEqual(_process_name_candidates(item), {"exampleapp", "exampleapp"})

    @patch("updater._winget_json")
    def test_winget_json_mapping(self, winget_json):
        winget_json.return_value = [{"Name": "Game", "Id": "Publisher.Game", "Version": "1.0", "AvailableVersion": "2.0"}]
        self.assertEqual(winget_updates()["game"], ("1.0", "2.0", "Publisher.Game"))

    def test_winget_table_fallback(self):
        with patch("updater._winget_json", return_value=[]), patch("updater._winget_table", return_value=[{"Name": "Game", "Id": "Publisher.Game", "Version": "1.0", "AvailableVersion": "2.0"}]):
            self.assertEqual(winget_updates()["game"], ("1.0", "2.0", "Publisher.Game"))

    def test_install_updates_runs_serially(self):
        items = [
            UpdateItem("First", "1.0", "2.0", package_id="Publisher.First"),
            UpdateItem("Second", "1.0", "2.0", package_id="Publisher.Second"),
        ]
        calls = []
        starts = []

        def fake_install(item, logger=None):
            calls.append(item.name)
            self.assertEqual(calls, starts)
            return True, "updated"

        with patch("updater.install_update", side_effect=fake_install):
            results = install_updates(items, on_start=lambda number, total, item: starts.append(item.name))

        self.assertEqual(calls, ["First", "Second"])
        self.assertEqual([result.success for result in results], [True, True])

    @patch("updater.is_admin", return_value=False)
    def test_install_requires_elevation(self, _admin):
        item = UpdateItem("Game", "1.0", "2.0", package_id="Publisher.Game")
        ok, detail = install_update(item)
        self.assertFalse(ok)
        self.assertIn("administrator", detail)

    def test_driver_process_shutdown_is_skipped(self):
        item = UpdateItem("Network Adapter", "1.0", "2.0", kind="driver")
        with patch("updater._run") as run:
            from updater import _close_application_processes
            detail = _close_application_processes(item)
        run.assert_not_called()
        self.assertIn("skipped", detail)

    def test_update_without_new_version_is_rejected(self):
        ok, detail = install_update(UpdateItem("Game", "2.0", "1.0", package_id="Publisher.Game"))
        self.assertFalse(ok)
        self.assertIn("newer version", detail)


if __name__ == "__main__":
    unittest.main()
