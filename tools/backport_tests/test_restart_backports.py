"""Run production transition logic with the existing prebuilt library's key flags."""
import ast
import datetime
import enum
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]
MANAGER = Path(os.environ.get("SUNNY_TEST_MANAGER_SOURCE", ROOT / "openpilot/system/manager/manager.py"))
READY_KEYS = ("ControlsReady", "FirmwareQueryDone", "ObdMultiplexingChanged", "ObdMultiplexingEnabled")


def definition(path, name, namespace):
  tree = ast.parse(path.read_text())
  node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
  exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
  return namespace[name]


class EndOfReplay(Exception):
  pass


class TestRestart(unittest.TestCase):
  def run_cycle(self, pandad_first):
    flags = definition(ROOT / "openpilot/common/params.py", "ParamKeyFlag", {"IntFlag": enum.IntFlag})
    # These are the flags in the unchanged, compiled libparams_c.so.
    old_flags = {k: flags.CLEAR_ON_MANAGER_START | flags.CLEAR_ON_ONROAD_TRANSITION for k in (*READY_KEYS, "CarParams")}
    old_flags["JoystickDebugMode"] = flags.CLEAR_ON_MANAGER_START | flags.CLEAR_ON_OFFROAD_TRANSITION
    probes = []
    offroad = []

    with tempfile.TemporaryDirectory() as directory:
      class Params:
        def get(self, key, **kwargs):
          path = Path(directory, key)
          return path.read_bytes() if path.exists() else None

        def get_bool(self, key):
          return self.get(key) == b"1"

        def put(self, key, value, **kwargs):
          Path(directory, key).write_bytes(value if isinstance(value, bytes) else str(value).encode())

        def put_bool(self, key, value, **kwargs):
          self.put(key, b"1" if value else b"0")

        def remove(self, key):
          Path(directory, key).unlink(missing_ok=True)

        def clear_all(self, flag):
          for key, mask in old_flags.items():
            if mask & flag:
              self.remove(key)

      params = Params()
      params.put("DongleId", "test")
      params.put("CarParamsPersistent", b"saved vehicle")

      def ready():
        return params.get_bool("FirmwareQueryDone") and params.get_bool("ControlsReady")

      class SubMaster:
        def __init__(self, *args, **kwargs):
          self.index = -1
          self.values = {
            "deviceState": SimpleNamespace(started=False),
            "carParams": SimpleNamespace(),
            "pandaStates": [SimpleNamespace(pandaType=1, ignitionLine=True, ignitionCan=False)],
          }

        def __getitem__(self, key):
          return self.values[key]

        def all_checks(self, *args):
          return False

        def update(self, *args):
          self.index += 1
          if self.index == 3:
            raise EndOfReplay
          self.values["deviceState"].started = (True, False, True)[self.index]
          if self.index == 2 and pandad_first:
            probes.append((ready(), params.get("CarParams")))

      launches = 0

      def ensure_running(processes, started, **kwargs):
        nonlocal launches
        if not started and launches:
          offroad.append({key: params.get(key) for key in (*READY_KEYS, "CarParams", "CarParamsPersistent", "JoystickDebugMode")})
        if started:
          launches += 1
          if launches == 2:
            if not pandad_first:
              probes.append((ready(), params.get("CarParams")))
            self.assertFalse(ready())
            params.put_bool("FirmwareQueryDone", True)
            self.assertFalse(ready(), "firmware alone must not enable car safety")
          for key in READY_KEYS:
            params.put_bool(key, True)
          params.put("CarParams", b"previous drive" if launches == 1 else b"new drive")
          params.put_bool("JoystickDebugMode", True)

      namespace = {
        "datetime": datetime, "os": os, "time": time, "cloudlog": Mock(), "Params": Params,
        "ParamKeyFlag": flags, "UNREGISTERED_DONGLE_ID": "unregistered", "managed_processes": {},
        "ensure_running": ensure_running,
        "log": SimpleNamespace(PandaState=SimpleNamespace(PandaType=SimpleNamespace(unknown=0))),
        "messaging": SimpleNamespace(SubMaster=SubMaster, PubMaster=Mock(),
                                     new_message=lambda *a, **k: SimpleNamespace(managerState=SimpleNamespace())),
      }
      manager_thread = definition(MANAGER, "manager_thread", namespace)
      with self.assertRaises(EndOfReplay):
        manager_thread()
      self.assertEqual(len(offroad), 1)
      for key in READY_KEYS:
        self.assertIsNone(offroad[0][key], key)
      self.assertIsNone(offroad[0]["JoystickDebugMode"])
      self.assertEqual(offroad[0]["CarParams"], b"previous drive")
      self.assertEqual(offroad[0]["CarParamsPersistent"], b"saved vehicle")
      self.assertEqual([p[0] for p in probes], [False])
      self.assertTrue(ready())
      self.assertEqual(params.get("CarParams"), b"new drive")

  def test_pandad_observes_restart_before_manager(self):
    self.run_cycle(pandad_first=True)

  def test_manager_observes_restart_before_pandad(self):
    self.run_cycle(pandad_first=False)


class TestCarEvents(unittest.TestCase):
  def test_radar_faults_remain_valid_messages(self):
    path = ROOT / "openpilot/selfdrive/car/card.py"
    tree = ast.parse(path.read_text())
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and
                      any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and
                          t.value.id == "tracks_msg" and t.attr == "valid" for t in n.targets))
    code = compile(ast.Module(body=[assignment], type_ignores=[]), str(path), "exec")
    for can_error in (False, True):
      for radar_fault in (False, True):
        for temporary_error in (False, True):
          with self.subTest(can_error=can_error, radar_fault=radar_fault, temporary_error=temporary_error):
            errors = SimpleNamespace(canError=can_error, radarFault=radar_fault, temporaryError=temporary_error)
            errors.to_dict = lambda: {"canError": can_error, "radarFault": radar_fault, "temporaryError": temporary_error}
            message = SimpleNamespace()
            exec(code, {"RD": SimpleNamespace(errors=errors), "tracks_msg": message})
            self.assertEqual(message.valid, not can_error)

  def test_unrecognized_car_alerts_respect_dashcam_mode(self):
    path = ROOT / "openpilot/selfdrive/selfdrived/selfdrived.py"
    cls = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "SelfdriveD")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    start = next(i for i, n in enumerate(init.body) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and
                 isinstance(n.value.func, ast.Name) and n.value.func.id == "set_offroad_alert" and
                 isinstance(n.value.args[0], ast.Constant) and n.value.args[0].value == "Offroad_CarUnrecognized")
    code = compile(ast.Module(body=init.body[start:start + 2], type_ignores=[]), str(path), "exec")
    for recognized in (False, True):
      for enabled in (False, True):
        for passive in (False, True):
          with self.subTest(recognized=recognized, enabled=enabled, passive=passive):
            events, alerts = Mock(), Mock()
            exec(code, {"self": SimpleNamespace(CP=SimpleNamespace(passive=passive), events=events),
                        "car_recognized": recognized, "openpilot_enabled": enabled,
                        "set_offroad_alert": alerts, "EventName": SimpleNamespace(carUnrecognized="unknown", dashcamMode="dashcam")})
            alerts.assert_called_once_with("Offroad_CarUnrecognized", not recognized)
            if not recognized and enabled:
              events.add.assert_called_once_with("unknown", static=True)
            elif passive:
              events.add.assert_called_once_with("dashcam", static=True)
            else:
              events.add.assert_not_called()


if __name__ == "__main__":
  unittest.main()
