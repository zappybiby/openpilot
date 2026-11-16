"""Run calibration logic on the host; only messaging, params, and hardware are faked."""
import ast
import contextlib
import json
import os
from pathlib import Path
import random
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from openpilot.common.constants import CV
from openpilot.common.transformations.orientation import rot_from_euler, euler_from_rot


class MemoryParams:
  values = {}

  def get(self, key, **kwargs):
    return self.values.get(key)

  def put(self, key, value, **kwargs):
    self.values[key] = value

  def remove(self, key):
    self.values.pop(key, None)


class Message:
  def __init__(self, data=None):
    defaults = dict(validBlocks=0, rpyCalib=[], height=[], wideFromDeviceEuler=[])
    defaults.update(data or {})
    self.extrinsicsCalibration = SimpleNamespace(**defaults)

  def to_bytes(self):
    return json.dumps(vars(self.extrinsicsCalibration)).encode()


class StopMain(Exception):
  pass


def load_production():
  source = Path(os.environ.get('BACKPORT_CALIBRATION_SOURCE',
                ROOT / 'openpilot/selfdrive/locationd/calibrationd.py'))
  tree = ast.parse(source.read_text())
  tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
  # Postponed annotations keep native capnp type annotations out of host execution.
  tree.body.insert(0, ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0))
  ns = dict(__name__='calibration_under_test', np=np, os=os, CV=CV,
            HARDWARE=SimpleNamespace(get_device_type=lambda: 'mici'),
            Params=MemoryParams, cloudlog=Mock(), config_realtime_process=lambda *args: None,
            rot_from_euler=rot_from_euler, euler_from_rot=euler_from_rot)
  status = SimpleNamespace(uncalibrated=0, calibrated=1, invalid=2, recalibrating=3)
  ns['log'] = SimpleNamespace(ExtrinsicsCalibration=SimpleNamespace(Status=status),
    Event=SimpleNamespace(from_bytes=lambda b: contextlib.nullcontext(Message(json.loads(b)))))
  ns['messaging'] = SimpleNamespace(new_message=lambda *args, **kwargs: Message())
  exec(compile(ast.fix_missing_locations(tree), str(source), 'exec'), ns)
  return ns


PRODUCTION = load_production()


class IsolatedTestCase(unittest.TestCase):
  def setUp(self):
    MemoryParams.values.clear()


# Reuse the complete upstream numerical suite with the production implementation.
upstream_tests = ROOT / 'openpilot/selfdrive/locationd/test/test_calibrationd.py'
test_tree = ast.parse(upstream_tests.read_text())
test_tree.body = [n for n in test_tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
test_namespace = dict(PRODUCTION, random=random, OpenpilotTestCase=IsolatedTestCase)
exec(compile(test_tree, str(upstream_tests), 'exec'), test_namespace)
TestUpstreamCalibration = test_namespace['TestCalibrationd']
process_messages = test_namespace['process_messages']
Calibrator = PRODUCTION['Calibrator']
BLOCK_SIZE = PRODUCTION['BLOCK_SIZE']
INPUTS_NEEDED = PRODUCTION['INPUTS_NEEDED']


class TestCalibrationBackports(IsolatedTestCase):
  def test_first_block_seeds_warp_without_becoming_saved_calibration(self):
    c = Calibrator(param_put=True)
    self.assertEqual(c.get_msg(True).extrinsicsCalibration.calPerc, 0)
    process_messages(c, [0.0, 0.10, 0.0], BLOCK_SIZE)
    self.assertTrue(c.seeded)
    self.assertEqual(c.valid_blocks, 0)
    self.assertEqual(c.block_idx, 0)
    self.assertGreater(c.rpy[1], 0.09)
    self.assertNotIn('CalibrationParams', MemoryParams.values)
    self.assertEqual(c.get_msg(True).extrinsicsCalibration.calPerc, 16)
    process_messages(c, [0.0, 0.0, 0.0], BLOCK_SIZE)
    self.assertEqual(c.valid_blocks, 1)
    self.assertEqual(c.block_idx, 1)

  def test_new_calibration_progress_is_monotonic_and_completes(self):
    c = Calibrator()
    percentages = [c.get_msg(True).extrinsicsCalibration.calPerc]
    for _ in range((INPUTS_NEEDED + 2) * BLOCK_SIZE):
      process_messages(c, [0.0, 0.0, 0.0], 1)
      percentages.append(c.get_msg(True).extrinsicsCalibration.calPerc)
    self.assertEqual(percentages, sorted(percentages))
    self.assertEqual(percentages[INPUTS_NEEDED * BLOCK_SIZE], 83)
    self.assertEqual(percentages[(INPUTS_NEEDED + 1) * BLOCK_SIZE], 100)
    self.assertLessEqual(max(percentages), 100)

  def test_saved_calibration_does_not_reseed(self):
    MemoryParams().put('CalibrationParams', Message(dict(validBlocks=5, rpyCalib=[0.0, 0.05, 0.0],
      height=[1.22], wideFromDeviceEuler=[0.0, 0.0, 0.0])).to_bytes())
    c = Calibrator(param_put=True)
    self.assertTrue(c.seeded)
    process_messages(c, [0.0, 0.0, 0.0], BLOCK_SIZE)
    self.assertEqual(c.block_idx, 1)
    self.assertEqual(c.valid_blocks, 5)

  def test_vehicle_change_resets_calibration_before_loading_it(self):
    for old_name, new_name, old_brand, new_brand, reset in [
      ('car-a', 'car-b', 'subaru', 'subaru', True),
      ('car-a', 'car-a', 'subaru', 'subaru', False),
      ('car-a', 'car-b', 'mock', 'subaru', False),
      ('car-a', 'car-b', 'subaru', 'mock', False),
      (None, 'car-b', 'subaru', 'subaru', False),
    ]:
      with self.subTest(old=old_name, new=new_name, old_brand=old_brand, new_brand=new_brand):
        MemoryParams.values.clear()
        MemoryParams.values['CalibrationParams'] = Message(dict(validBlocks=5, rpyCalib=[0.0, 0.05, 0.0],
          height=[1.22], wideFromDeviceEuler=[0.0, 0.0, 0.0])).to_bytes()
        if old_name is not None:
          MemoryParams.values['CarParamsPrevRoute'] = b'old-car'
        current = SimpleNamespace(carFingerprint=new_name, brand=new_brand, notCar=False)
        previous = SimpleNamespace(carFingerprint=old_name, brand=old_brand)
        observed = []
        def capture_calibrator(**kwargs):
          observed.append(MemoryParams().get('CalibrationParams'))
          return Calibrator(**kwargs)
        sm = SimpleNamespace(frame=-1, update=Mock(side_effect=StopMain))
        messaging = SimpleNamespace(PubMaster=lambda *args: None, SubMaster=lambda *args, **kwargs: sm,
          log_from_bytes=lambda *args: current, new_message=lambda *args: Message())
        car = SimpleNamespace(CarParams=SimpleNamespace(from_bytes=lambda *args: contextlib.nullcontext(previous)))
        with unittest.mock.patch.dict(PRODUCTION, messaging=messaging, car=car, Calibrator=capture_calibrator):
          with self.assertRaises(StopMain):
            PRODUCTION['main']()
        self.assertEqual(observed[0] is None, reset)


if __name__ == '__main__':
  unittest.main()
