"""Run selected production methods without the device's native UI libraries."""

import ast
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[2]


def source_node(path, *names):
  node = ast.parse((ROOT / path).read_text(), filename=path)
  for name in names:
    node = next(child for child in node.body if isinstance(child, (ast.ClassDef, ast.FunctionDef)) and child.name == name)
  return node


def source_function(path, names, namespace):
  node = source_node(path, *names)
  module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
  exec(compile(ast.fix_missing_locations(module), path, "exec"), namespace)
  return namespace[node.name]


class TestSetupHttpErrors(unittest.TestCase):
  def test_http_failures_leave_download_screen_with_a_reason(self):
    url = "https://installer.comma.ai/zappybiby/sunny-dev"
    for code, expected in ((404, "Server error (404): installer.comma.ai/zappybiby/sunny-dev"),
                           (500, "Server error (500): installer.comma.ai/zappybiby/sunny-dev"),
                           (409, "Incompatible sunnypilot version.")):
      with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
        opener = Mock(side_effect=urllib.error.HTTPError(url, code, "test response", {}, None))
        namespace = {
          "urllib": SimpleNamespace(error=urllib.error, request=SimpleNamespace(Request=urllib.request.Request, urlopen=opener)),
          "USER_AGENT": "AGNOSSetup-19.9",
          "HARDWARE": SimpleNamespace(get_serial=lambda: "test", get_device_type=lambda: "mici"),
        }
        download = source_function("openpilot/system/ui/mici_setup.py", ("Setup", "_download_thread"), namespace)
        setup = SimpleNamespace(download_url=url, _download_failed_reason=None)
        # The error path never uses the descriptor; keep all file I/O inside this test directory.
        with patch("tempfile.mkstemp", return_value=(-1, str(Path(directory) / "installer"))):
          download(setup)
        self.assertEqual(setup._download_failed_reason, expected)
        self.assertEqual(opener.call_args.args[0].full_url, url)
        self.assertEqual(opener.call_args.args[0].get_header("X-openpilot-device-type"), "mici")


class TestHotspotPassword(unittest.TestCase):
  def test_password_boundaries_control_actual_dialog_confirmation(self):
    dialog_factory = Mock(return_value=object())
    shown = Mock()
    write_password = Mock()
    namespace = {
      "self": SimpleNamespace(_wifi_manager=SimpleNamespace(tethering_password="original-password")),
      "BigInputDialog": dialog_factory,
      "tethering_password_callback": write_password,
      "gui_app": SimpleNamespace(push_widget=shown),
    }
    click = source_function("openpilot/selfdrive/ui/mici/layouts/settings/network/network_layout.py",
                            ("NetworkLayoutMici", "__init__", "tethering_password_clicked"), namespace)
    click()
    shown.assert_called_once_with(dialog_factory.return_value)
    options = dialog_factory.call_args.kwargs

    dialog_path = "openpilot/selfdrive/ui/mici/widgets/dialog.py"
    constructor = source_node(dialog_path, "BigInputDialog", "__init__")
    validation = next(node.value for node in constructor.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Attribute) and target.attr == "_text_valid" for target in node.targets))
    validate = eval(compile(ast.Expression(validation), dialog_path, "eval"),
                    {"minimum_length": options["minimum_length"], "text_validator": options.get("text_validator")})

    for length, accepted in ((0, False), (7, False), (8, True), (63, True), (64, False)):
      with self.subTest(length=length):
        value = "a" * length
        write_password.reset_mock()
        dialog = SimpleNamespace(_text_valid=validate, _keyboard=SimpleNamespace(text=lambda: value),
                                 dismiss=lambda callback: callback())
        confirm = source_function(dialog_path, ("BigInputDialog", "__init__", "confirm_callback_wrapper"),
                                  {"self": dialog, "confirm_callback": options["confirm_callback"]})
        confirm()
        if accepted:
          write_password.assert_called_once_with(value)
        else:
          write_password.assert_not_called()


class TestWifiDbusRecovery(unittest.TestCase):
  def make_manager(self, first_error=None, second_error=None):
    register = Mock()
    log_exception = Mock()
    namespace = {
      "DBusRouter": lambda connection: connection,
      "open_dbus_connection_threading": Mock(return_value=object(), side_effect=first_error),
      "open_dbus_connection_blocking": Mock(return_value=object(), side_effect=second_error),
      "_wrap_router": lambda router: None,
      "DBusAddress": Mock(), "NM_PATH": "/test", "NM": "test", "NM_IFACE": "test",
      "cloudlog": SimpleNamespace(exception=log_exception),
      "WifiState": SimpleNamespace,
      "MeteredType": SimpleNamespace(UNKNOWN=0),
      "Params": None,
      "threading": SimpleNamespace(Lock=threading.Lock, Thread=Mock()),
      "atexit": SimpleNamespace(register=register),
    }
    initialize = source_function("openpilot/system/ui/lib/wifi_manager.py", ("WifiManager", "__init__"), namespace)
    manager = SimpleNamespace(_network_scanner=Mock(), _monitor_state=Mock(), _initialize=Mock(), stop=Mock())
    initialize(manager)
    register.assert_called_once_with(manager.stop)
    return manager, log_exception

  def test_connection_failures_disable_wifi_manager_without_crashing_ui(self):
    for first, second in ((TimeoutError("D-Bus timed out"), None), (None, RuntimeError("D-Bus unavailable"))):
      with self.subTest(first=first, second=second):
        manager, logger = self.make_manager(first, second)
        self.assertTrue(manager._exit)
        self.assertIsNone(manager._router_main)
        self.assertIsNone(manager._conn_monitor)
        manager._initialize.assert_not_called()
        logger.assert_called_once_with("Failed to connect to system D-Bus")

  def test_successful_connection_still_initializes_wifi(self):
    manager, logger = self.make_manager()
    self.assertFalse(manager._exit)
    manager._initialize.assert_called_once_with()
    logger.assert_not_called()


if __name__ == "__main__":
  unittest.main()
