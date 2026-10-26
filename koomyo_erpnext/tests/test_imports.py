"""Any sync module can be imported first (a hook or a job may start with any of them)."""

import subprocess
import sys

from koomyo_erpnext.compat import KoomyoTestCase

MODULES = (
	"sales",
	"refunds",
	"stock",
	"transfers",
	"order_stock",
	"purchasing",
	"customers",
	"catalog",
	"shifts",
	"backfill",
	"items",
	"common",
	"events",
)


class TestImports(KoomyoTestCase):
	def test_each_sync_module_imports_first(self):
		for module in MODULES:
			code = (
				f"import importlib; importlib.import_module('koomyo_erpnext.sync.{module}'); "
				"from koomyo_erpnext.sync.events import DISPATCH; assert 'order.paid' in DISPATCH"
			)
			result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
			self.assertEqual(result.returncode, 0, f"{module}: {result.stderr[-500:]}")
