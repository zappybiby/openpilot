import math
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
    self.set_rect(rl.Rectangle(0, 0, 402, 180))
    self._font = gui_app.font(FontWeight.BOLD)
    self._titles = [self._child(UnifiedLabel(text, font_size=28, alignment=TextAlignment.CENTER,
                                            alignment_vertical=TextAlignmentVertical.MIDDLE, wrap_text=False))
                    for text in ("LAF", "vs. default")]
    self._values = [self._child(UnifiedLabel("–", font_size=72, font_weight=FontWeight.BOLD, text_color=rl.WHITE,
                                            alignment=TextAlignment.CENTER, alignment_vertical=TextAlignmentVertical.MIDDLE,
                                            wrap_text=False)) for _ in range(2)]

  def _set_value(self, factor: float | None):
    for label, text in zip(self._values, format_laf(factor), strict=True):
      label.set_text(text)

  def show_event(self):
    super().show_event()
    if self._state.sm.seen[TORQUE_SERVICE]:
      self._update_state()
      return

    self._set_value(None)
    cached = self._state.params.get("LiveTorqueParameters")
    if cached:
      try:
        with log.Event.from_bytes(cached) as event:
          factor = event.lateralTorqueParameters.latAccelFactorFiltered
      except Exception:
        # A missing or damaged cache must not stop the settings menu from opening.
        return
      self._set_value(factor)

  def _update_state(self):
    if self._state.sm.seen[TORQUE_SERVICE]:
      self._set_value(self._state.sm[TORQUE_SERVICE].latAccelFactorFiltered)

  def _render(self, rect):
    column_width = rect.width / 2
    text_width = column_width - 24
    widest = max(measure_text_cached(self._font, label.text, 72).x for label in self._values)
    font_size = min(72, int(72 * text_width / max(widest, 1)))

    for i, (title, value) in enumerate(zip(self._titles, self._values, strict=True)):
      x = rect.x + i * column_width + 12
      title.render(rl.Rectangle(x, rect.y + 18, text_width, 40))
      value.set_font_size(font_size)
      value.render(rl.Rectangle(x, rect.y + 64, text_width, 94))

    rl.draw_line_ex(rl.Vector2(rect.x + column_width, rect.y + 24),
                    rl.Vector2(rect.x + column_width, rect.y + rect.height - 24), 1, rl.Color(90, 90, 90, 255))
