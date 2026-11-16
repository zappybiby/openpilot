"""Run with: python3 -m unittest discover -s tools/backport_tests -p test_rpc_backports.py.

The real dispatcher and production registration declarations run on any host.
Device services are stubbed; declarations are loaded without starting daemons or
importing the prebuilt ARM extensions.
"""

import ast
import base64
import gzip
import importlib.util
import json
import os
import re
import sys
import threading
import unittest
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import IntEnum
from functools import partial
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]


def load_file(name, relative_path):
  spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
  module = importlib.util.module_from_spec(spec)
  sys.modules[name] = module
  spec.loader.exec_module(module)
  return module


rpc = load_file("backport_rpc", "openpilot/system/athena/rpc.py")
services = load_file("backport_services", "openpilot/cereal/services.py")
helpers = load_file("backport_helpers", "openpilot/tools/lib/helpers.py")


def is_registration(node):
  if isinstance(node, ast.Call):
    node = node.func
  return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and \
    node.value.id == "dispatcher" and node.attr == "add_method"


def load_declarations(relative_path, namespace, constant_names, extra_functions=()):
  """Execute unchanged method bodies, signatures, decorators and registration calls."""
  path = ROOT / relative_path
  nodes = []
  for node in ast.parse(path.read_text()).body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constant_names for t in node.targets):
      nodes.append(node)
    elif isinstance(node, ast.FunctionDef) and (node.name in extra_functions or any(map(is_registration, node.decorator_list))):
      nodes.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == "VideoClips":
      nodes.append(node)
      # Keep the real methods, but skip the constructor's background worker.
      nodes.extend(ast.parse("video_clips = object.__new__(VideoClips)").body)
    elif isinstance(node, ast.Expr) and is_registration(node.value):
      nodes.append(node)
  tree = ast.Module(body=ast.parse("from __future__ import annotations").body + nodes, type_ignores=[])
  exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), namespace)


def registration_environment():
  module = ModuleType("backport_athena_declarations")
  sys.modules[module.__name__] = module
  env = module.__dict__
  env.update({name: value for name, value in vars(rpc).items() if not name.startswith("__")})
  env.update(dispatcher=rpc.Dispatcher(), RE=helpers.RE, SERVICE_LIST=services.SERVICE_LIST,
             dataclass=dataclass, asdict=asdict, datetime=datetime, partial=partial,
             threading=threading, re=re, os=os, PC=True, cast=cast)
  load_declarations("openpilot/system/athena/athenad.py", env, {
    "LOCAL_PORT_WHITELIST", "DEFAULT_UPLOAD_PRIORITY", "DATA_PATH", "UPLOAD_URL",
    "UPLOAD_HEADERS", "UPLOAD_ID", "CLIP_FILENAME", "UPLOAD_FILE",
  }, {"jsonrpc_handler", "startLocalProxy"})
  sunny = dict(env)
  load_declarations("openpilot/sunnypilot/sunnylink/athena/sunnylinkd.py", sunny, {
    "LOCAL_PORT_WHITELIST", "PARAM_KEY", "BASE64", "SUNNYLINK_PROXY_URI", "BLOCKED_PARAMS", "DISALLOW_LOG_UPLOAD",
  }, {"startLocalProxy"})
  return env, sunny


class TestDispatcher(unittest.TestCase):
  def setUp(self):
    self.methods = rpc.Dispatcher()
    self.calls = []

    @self.methods.add_method(count=rpc.integer(0, 10), enabled=rpc.boolean)
    def request(count=2, enabled=False):
      self.calls.append((count, enabled))
      return [count, enabled]

  def call(self, params=None):
    return json.loads(rpc.handle(rpc.dumps_call("request", params, 42), self.methods))

  def test_valid_positional_named_and_default_arguments(self):
    for params, expected in (([3, True], [3, True]), ({"enabled": True}, [2, True]), (None, [2, False])):
      with self.subTest(params=params):
        self.assertEqual(self.call(params), {"jsonrpc": "2.0", "id": 42, "result": expected})
    self.assertEqual(self.calls, [(3, True), (2, True), (2, False)])

  def test_bad_payloads_never_reach_method(self):
    for params in ([True], [1.0], [-1], [11], [None], ["2"], [float("nan")], [float("inf")],
                   [1, 1], [1, False, 2], {"unknown": 1}, "bad"):
      with self.subTest(params=params):
        self.assertEqual(self.call(params)["error"]["code"], rpc.INVALID_PARAMS)
    self.assertEqual(self.calls, [])

  def test_protocol_errors(self):
    for raw, code in (("{", rpc.PARSE_ERROR), (b"\xff", rpc.PARSE_ERROR), ("[]", rpc.PARSE_ERROR),
                      ({"method": 2}, rpc.INVALID_REQUEST), ({"method": "unknown"}, rpc.METHOD_NOT_FOUND)):
      with self.subTest(raw=raw):
        self.assertEqual(json.loads(rpc.handle(raw, self.methods))["error"]["code"], code)

  def test_registration_requires_every_parameter_and_preserves_direct_calls(self):
    def method(value=1):
      return value
    for validators in ({}, {"other": rpc.boolean}, {"value": rpc.boolean, "other": rpc.boolean}):
      with self.subTest(validators=validators), self.assertRaises(TypeError):
        self.methods.add_method(method, **validators)
    self.assertIs(self.methods.add_method(method, value=rpc.integer(0, 10)), method)
    self.assertEqual(method("direct"), "direct")
    with self.assertRaises(TypeError):
      self.methods["method"]("remote")
    with self.assertRaises(TypeError):
      self.methods["unsafe"] = method

  def test_composed_validator_types_and_numeric_bounds(self):
    validator = rpc.struct({"enabled": rpc.boolean, "amount": rpc.number(0, 10)}, {"tags": rpc.list_of(rpc.regex(r"\w+"))})
    self.assertTrue(validator({"enabled": True, "amount": 2.5, "tags": ["ascii_1"]}))
    for value in (float("nan"), float("inf"), -float("inf"), True, "2", -1, 11):
      with self.subTest(value=value):
        self.assertFalse(validator({"enabled": True, "amount": value}))
    for payload in ({"amount": 1}, {"enabled": True, "amount": 1, "extra": 0},
                    {"enabled": True, "amount": 1, "tags": ["é"]}):
      self.assertFalse(validator(payload))
    self.assertFalse(rpc.one_of(1)(True))
    self.assertFalse(rpc.dict_of(rpc.regex(r"\w+"), rpc.boolean)({"ok": 1}))


class TestProductionRegistrations(unittest.TestCase):
  def setUp(self):
    self.athena, self.sunny = registration_environment()
    self.methods = self.athena["dispatcher"]

  def call(self, method, params=None):
    return json.loads(rpc.handle(rpc.dumps_call(method, params, 7), self.methods))

  def assert_invalid(self, method, params):
    self.assertEqual(self.call(method, params)["error"]["code"], rpc.INVALID_PARAMS)

  def test_all_production_decorators_and_bound_methods_register(self):
    self.assertEqual(set(self.methods), {
      "getMessage", "getVersion", "listDataDirectory", "createClip", "getClipState", "deleteClip", "getClipChunk",
      "uploadFileToUrl", "uploadFilesToUrls", "listUploadQueue", "cancelUpload", "setNavDestination",
      "clearNavDestination", "setRouteViewed", "getPublicKey", "getSshAuthorizedKeys", "getGithubUsername",
      "getNotCar", "getSimInfo", "getNetworkType", "getNetworkMetered", "startStream", "toggleLogUpload",
      "getParamsAllKeys", "getParamsMetadata", "getParams", "saveParams",
    })

  def test_message_defaults_and_service_timeout_validation(self):
    messaging = SimpleNamespace(sub_sock=Mock(return_value=object()), recv_one=Mock(return_value=SimpleNamespace(to_dict=lambda: {"ok": True})))
    self.athena["messaging"] = messaging
    self.assertEqual(self.call("getMessage", {"service": "carState"})["result"], {"ok": True})
    messaging.sub_sock.assert_called_once_with("carState", timeout=1000)
    for params in ({"service": "unknown"}, {"service": "carState", "timeout": -1},
                   {"service": "carState", "timeout": True}, {"service": "carState", "timeout": 10001}):
      self.assert_invalid("getMessage", params)
    self.assertEqual(messaging.sub_sock.call_count, 1)

  def test_navigation_rejects_nonfinite_and_wrong_type_without_writes(self):
    params = Mock()
    self.athena["Params"] = lambda: params
    self.assertEqual(self.call("setNavDestination", [45.5, -122.5])["result"], {"success": 1})
    params.put.assert_called_once_with("NavDestination", {"latitude": 45.5, "longitude": -122.5}, block=True)
    for coords in ([91, 0], [0, -181], [float("nan"), 0], [0, float("inf")], [True, 0], [0, "1"]):
      self.assert_invalid("setNavDestination", coords)
    self.assertEqual(params.put.call_count, 1)

  def test_upload_paths_headers_and_nested_files(self):
    good = {"fn": "2026-11-15--12-30-00--0/qlog.zst", "url": "https://upload.example/file", "headers": {"Authorization": "Bearer token"}}
    self.assertTrue(self.athena["UPLOAD_FILE"](good))
    self.assertTrue(self.athena["UPLOAD_FILE"](good | {"allow_cellular": True, "priority": 0}))
    changes = ({"fn": "../secret"}, {"fn": "/absolute"}, {"fn": "route/../../secret"},
               {"url": "file:///tmp/log"}, {"headers": {"X-Test": "bad\r\nHeader: value"}},
               {"priority": True}, {"priority": 100}, {"allow_cellular": 1}, {"unexpected": 1})
    for change in changes:
      with self.subTest(change=change):
        self.assert_invalid("uploadFilesToUrls", {"files_data": [good | change]})
    self.assert_invalid("uploadFilesToUrls", {"files_data": [{"fn": "log"}]})
    self.assert_invalid("uploadFilesToUrls", {"files_data": good})
    self.assert_invalid("cancelUpload", {"upload_id": ["not-a-sha1"]})

  def test_clip_defaults_and_validation(self):
    clips = self.athena["video_clips"]
    clips.lock = threading.Condition()
    clips.clips = {}
    clips.transcode_proc = None
    clips._on_disk = lambda: {}
    self.assertEqual(self.call("getClipState")["result"], {"clips": []})
    route = "0123456789abcdef|2026-11-15--12-30-00"
    clip = {"camera": "fcamera.hevc", "filename": "clip.mp4", "bitrate": 5, "speedup": 1}
    self.assertIsNone(self.call("createClip", [route, 0, 60, clip])["result"])
    self.assertEqual(clips.clips["clip.mp4"].source_end_time, 60)
    for change in ({"filename": "../clip.mp4"}, {"speedup": True}, {"speedup": 11},
                   {"bitrate": float("nan")}, {"camera": "../fcamera.hevc"}, {"extra": 1}):
      self.assert_invalid("createClip", [route, 0, 60, clip | change])
    self.assert_invalid("createClip", [route, 0, float("inf"), clip])
    self.assert_invalid("getClipChunk", ["clip.mp4", -1])
    self.assert_invalid("getClipChunk", ["clip.mp4", True])

  def test_sunnylink_defaults_and_parameter_validation(self):
    class ParamKeyType(IntEnum):
      STRING, BOOL, INT, FLOAT, TIME, JSON, BYTES = range(7)
    params = Mock()
    params.all_keys.return_value = [b"TestParam"]
    params.get_type.return_value = ParamKeyType.STRING
    params.get.return_value = "0"
    save = Mock()
    self.sunny.update(Params=lambda: params, params=params, ParamKeyType=ParamKeyType,
                      get_param_as_byte=lambda key: b"hello", base64=base64, gzip=gzip,
                      save_param_from_base64_encoded_string=save, cloudlog=Mock())
    self.assertEqual(self.call("getParams", {"params_keys": ["TestParam"]})["result"]["TestParam"], "aGVsbG8=")
    self.assertIsNone(self.call("saveParams", {"params_to_update": {"TestParam": "aGVsbG8="}})["result"])
    save.assert_called_once_with("TestParam", "aGVsbG8=", False)
    for payload in ({"params_to_update": {"../key": "aGVsbG8="}}, {"params_to_update": {"TestParam": "invalid!"}},
                    {"params_to_update": {"TestParam": 1}}, {"params_to_update": {}, "compression": 1}):
      self.assert_invalid("saveParams", payload)
    self.assert_invalid("getParams", {"params_keys": "TestParam"})
    self.assert_invalid("getParams", {"params_keys": ["TestParam"], "compression": "false"})
    self.assert_invalid("toggleLogUpload", [1])
    self.assertEqual(save.call_count, 1)

  def test_dynamic_proxy_registration_and_redirect_limit(self):
    end = threading.Event()
    end.set()
    self.athena.update(Params=lambda: Mock(get=lambda key: "dongle"), Api=lambda dongle: Mock(get_token=lambda: "token"),
                       cloudlog=Mock(), create_connection=Mock(return_value="socket"),
                       start_local_proxy_shim=Mock(return_value={"success": 1}))
    self.athena["jsonrpc_handler"](end)
    self.assertEqual(self.call("startLocalProxy", ["wss://ssh.comma.ai/0123456789abcdef", 22])["result"], {"success": 1})
    self.assertEqual(self.athena["create_connection"].call_args.kwargs["redirect_limit"], 0)
    for uri, port in (("wss://evil.example/0123456789abcdef", 22), ("wss://ssh.comma.ai/0123456789abcdef", 80),
                      ("wss://ssh.comma.ai.evil.example/0123456789abcdef", 22)):
      self.assert_invalid("startLocalProxy", [uri, port])

    # The sunnylink handler supplies its own host validator and partial function.
    self.sunny.update(params=Mock(get=lambda key: "dongle"), SunnylinkApi=lambda dongle: Mock(get_token=lambda: "token"),
                      cloudlog=Mock(), create_connection=Mock(return_value="socket"),
                      ssl=SimpleNamespace(CERT_NONE=0), start_local_proxy_shim=Mock(return_value={"success": 1}))
    self.athena["jsonrpc_handler"](end, partial(self.sunny["startLocalProxy"], end), self.sunny["SUNNYLINK_PROXY_URI"])
    self.assertEqual(self.call("startLocalProxy", ["wss://ssh.sunnylink.ai/session", 8022])["result"], {"success": 1})
    for uri in ("wss://sunnylink.ai.evil.example/session", "wss://evil.example/sunnylink.ai", "ws://ssh.sunnylink.ai/session"):
      self.assert_invalid("startLocalProxy", [uri, 8022])
    self.assertEqual(self.sunny["create_connection"].call_count, 1)


if __name__ == "__main__":
  unittest.main()
