from openpilot.selfdrive.ui.mici.widgets.lateral_accel_factor import LateralAccelFactorTile
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.widgets.nav_widget import NavWidget
from openpilot.system.ui.widgets.scroller import Scroller


class ValuesLayoutMici(NavWidget):
  def __init__(self):
    super().__init__()
    self._pages = self._child(Scroller(snap_items=True, spacing=0, pad=0, scroll_indicator=False, edge_shadows=False))
    self._pages.set_enabled(lambda: self.enabled and not self.is_dismissing)
    self._pages._scroller.add_widget(LateralAccelFactorTile(ui_state))

  def _render(self, rect):
    self._pages.render(rect)
