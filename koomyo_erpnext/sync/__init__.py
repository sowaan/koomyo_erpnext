# Load the event processor first: it imports every sync module in a safe order and registers
# their handlers, so importing any one sync module (sales, stock, ...) on its own always works.
from koomyo_erpnext.sync import events
