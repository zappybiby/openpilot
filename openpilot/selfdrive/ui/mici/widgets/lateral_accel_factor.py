import math
import time
from typing import TYPE_CHECKING

import pyray as rl

from openpilot.cereal import log
from openpilot.system.ui.lib.application import gui_app, FontWeight, TextAlignment, TextAlignmentVertical
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.label import UnifiedLabel

if TYPE_CHECKING:
  from openpilot.selfdrive.ui.ui_state import UIState

# Stock SUBARU_IMPREZA_2020 tune, which includes the 2020 Crosstrek.
CROSSTREK_LAT_ACCEL_FACTOR = 2.6068223389108303
TORQUE_SERVICE = "lateralTorqueParameters"
DISPLAY_UPDATE_INTERVAL = 2.0
VALUE_FONT_SIZE = 96


def format_laf(factor: float | None) -> tuple[str, str]:
  if factor is None or not math.isfinite(factor) or factor <= 0:
    return "–", "–"
  deviation = (factor / CROSSTREK_LAT_ACCEL_FACTOR - 1) * 100
  if not math.isfinite(deviation):
    return "–", "–"
  percent = round(deviation)
  return f"{factor:.2f}", f"{percent:+d}%" if percent else "0%"


class LateralAccelFactorTile(Widget):
  def __init__(self, state: "UIState"):
    super().__init__()
    self._state = state
    self._next_update = 0.0
    self.set_rect(rl.Rectangle(0, 0, gui_app.width, gui_app.height))
    self._font = gui_app.font(FontWeight.BOLD)
    self._titles = [self._child(UnifiedLabel(text, font_size=28, alignment=TextAlignment.CENTER,
                                            alignment_vertical=TextAlignmentVertical.MIDDLE, wrap_text=False))
                    for text in ("LAF", "vs. default")]
    self._values = [self._child(UnifiedLabel("–", font_size=VALUE_FONT_SIZE, font_weight=FontWeight.BOLD, text_color=rl.WHITE,
                                            alignment=TextAlignment.CENTER, alignment_vertical=TextAlignmentVertical.MIDDLE,
                                            wrap_text=False)) for _ in range(2)]

  def _set_value(self, factor: float | None):
    for label, text in zip(self._values, format_laf(factor), strict=True):
      label.set_text(text)

  def show_event(self):
    super().show_event()
    self._next_update = 0.0
    self._update_state()

  def _read_cache(self) -> float | None:
    cached = self._state.params.get("LiveTorqueParameters")
    if cached:
      try:
        with log.Event.from_bytes(cached) as event:
          return event.lateralTorqueParameters.latAccelFactorFiltered
      except Exception:
        # A missing or damaged cache must not stop the settings menu from opening.
        pass
    return None

  def _update_state(self):
    now = time.monotonic()
    if now < self._next_update:
      return
    self._next_update = now + DISPLAY_UPDATE_INTERVAL

    if self._state.started:
      sm = self._state.sm
      # A previous drive's last message remains in SubMaster across onroad cycles.
      current = sm.seen[TORQUE_SERVICE] and sm.alive[TORQUE_SERVICE] and sm.recv_frame[TORQUE_SERVICE] > self._state.started_frame
      self._set_value(sm[TORQUE_SERVICE].latAccelFactorFiltered if current else None)
    else:
      self._set_value(self._read_cache())

  def _render(self, rect):
    column_width = rect.width / 2
    text_width = column_width - 24
    widest = max(measure_text_cached(self._font, label.text, VALUE_FONT_SIZE).x for label in self._values)
    font_size = min(VALUE_FONT_SIZE, int(VALUE_FONT_SIZE * text_width / max(widest, 1)))

    for i, (title, value) in enumerate(zip(self._titles, self._values, strict=True)):
      x = rect.x + i * column_width + 12
      title.render(rl.Rectangle(x, rect.y + 30, text_width, 40))
      value.set_font_size(font_size)
      value.render(rl.Rectangle(x, rect.y + 80, text_width, 126))

    rl.draw_line_ex(rl.Vector2(rect.x + column_width, rect.y + 36),
                    rl.Vector2(rect.x + column_width, rect.y + rect.height - 30), 1, rl.Color(90, 90, 90, 255))
