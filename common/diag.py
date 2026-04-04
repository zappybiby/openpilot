import json
import time
from typing import Any


def mono_time_ns() -> int:
  return time.monotonic_ns()


def _format_field(value: Any) -> str:
  if value is None:
    return "null"
  if isinstance(value, bool):
    return "1" if value else "0"
  if isinstance(value, int):
    return str(value)
  if isinstance(value, float):
    return f"{value:.6f}"
  if isinstance(value, str):
    if value and all(c.isalnum() or c in "._:/-@" for c in value):
      return value
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'))
  return json.dumps(value, ensure_ascii=True, separators=(',', ':'), sort_keys=True)


def kmsg_log(component: str, event: str, *, mono_ns: int | None = None,
             level: int = 6, **fields: Any) -> int:
  ts = mono_ns if mono_ns is not None else mono_time_ns()
  parts = [f"event={_format_field(event)}", f"mono_ns={ts}"]
  parts.extend(f"{key}={_format_field(value)}" for key, value in sorted(fields.items()))

  try:
    with open("/dev/kmsg", "w") as kmsg:
      kmsg.write(f"<{level}>[{component}] {' '.join(parts)}\n")
  except OSError:
    pass

  return ts
