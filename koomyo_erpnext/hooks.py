app_name = "koomyo_erpnext"
app_title = "Koomyo ERPNext"
app_publisher = "Sowaan"
app_description = " Sync your Koomyo restaurant sales, stock and purchasing with ERPNext, automatically."
app_email = "info@sowaan.com"
app_license = "mit"
app_logo_url = "/assets/koomyo_erpnext/images/koomyo-icon.png"

# Tile on the /apps screen, for the people who run the integration.
add_to_apps_screen = [
	{
		"name": "koomyo_erpnext",
		"logo": "/assets/koomyo_erpnext/images/koomyo-icon.png",
		"title": "Koomyo",
		"route": "/app/koomyo",
		"has_permission": "koomyo_erpnext.dashboard.has_app_permission",
	}
]

# Apps
# ------------------

required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "koomyo_erpnext",
# 		"logo": "/assets/koomyo_erpnext/logo.png",
# 		"title": "Koomyo Erpnext",
# 		"route": "/koomyo_erpnext",
# 		"has_permission": "koomyo_erpnext.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/koomyo_erpnext/css/koomyo_erpnext.css"
# app_include_js = "/assets/koomyo_erpnext/js/koomyo_erpnext.js"

# include js, css files in header of web template
# web_include_css = "/assets/koomyo_erpnext/css/koomyo_erpnext.css"
# web_include_js = "/assets/koomyo_erpnext/js/koomyo_erpnext.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "koomyo_erpnext/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "koomyo_erpnext/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "koomyo_erpnext.utils.jinja_methods",
# 	"filters": "koomyo_erpnext.utils.jinja_filters"
# }

# Installation
# ------------

before_install = "koomyo_erpnext.install.before_install"
after_install = "koomyo_erpnext.install.after_install"
before_migrate = "koomyo_erpnext.install.before_migrate"
after_migrate = "koomyo_erpnext.install.after_migrate"

# Uninstallation
# ------------

# before_uninstall = "koomyo_erpnext.uninstall.before_uninstall"
# after_uninstall = "koomyo_erpnext.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "koomyo_erpnext.utils.before_app_install"
# after_app_install = "koomyo_erpnext.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "koomyo_erpnext.utils.before_app_uninstall"
# after_app_uninstall = "koomyo_erpnext.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "koomyo_erpnext.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["koomyo_erpnext.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"koomyo_erpnext.tasks.all"
# 	],
# 	"daily": [
# 		"koomyo_erpnext.tasks.daily"
# 	],
# 	"hourly": [
# 		"koomyo_erpnext.tasks.hourly"
# 	],
# 	"weekly": [
# 		"koomyo_erpnext.tasks.weekly"
# 	],
# 	"monthly": [
# 		"koomyo_erpnext.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "koomyo_erpnext.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "koomyo_erpnext.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "koomyo_erpnext.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["koomyo_erpnext.utils.before_request"]
# after_request = ["koomyo_erpnext.utils.after_request"]

# Job Events
# ----------
# before_job = ["koomyo_erpnext.utils.before_job"]
# after_job = ["koomyo_erpnext.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"koomyo_erpnext.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []


# Koomyo
# ------

scheduler_events = {
	"cron": {
		# Read each connection's events feed and process what is due (one job per connection).
		"*/5 * * * *": ["koomyo_erpnext.sync.events.schedule_catch_up"],
	},
	"daily": [
		"koomyo_erpnext.sync.events.cleanup_event_log",
		"koomyo_erpnext.sync.events.daily_alerts",
	],
}

# Custom fields koomyo_connection / koomyo_id / koomyo_branch on every doctype the app writes.
fixtures = [{"dt": "Custom Field", "filters": [["module", "=", "Koomyo Erpnext"]]}]

# ERPNext documents that are written to Koomyo (queued here, sent by the background processor).
doc_events = {
	"Purchase Order": {"on_submit": "koomyo_erpnext.sync.purchasing.on_purchase_order_submit"},
	"Purchase Receipt": {"on_submit": "koomyo_erpnext.sync.purchasing.on_purchase_receipt_submit"},
	"Supplier": {"on_update": "koomyo_erpnext.sync.purchasing.on_supplier_update"},
	"Item": {"on_update": "koomyo_erpnext.sync.purchasing.on_item_update"},
}
