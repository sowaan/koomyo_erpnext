// Copyright (c) 2026, Sowaan and contributors
// For license information, please see license.txt

frappe.query_reports["Koomyo Sales by Payment Method and Tax Rate"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "connection",
			label: __("Koomyo Connection"),
			fieldtype: "Link",
			options: "Koomyo Connection",
		},
		{ fieldname: "branch", label: __("Koomyo Branch Id"), fieldtype: "Data" },
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.month_start(),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "basis",
			label: __("Date Basis"),
			fieldtype: "Select",
			options: "Posting date\nBusiness day",
			default: "Posting date",
			description: __(
				"Business day is the trading day Koomyo gave each order: a sale at 01:30 belongs to the day before when the day starts at 09:00."
			),
		},
		{ fieldname: "by_day", label: __("Show Each Day"), fieldtype: "Check", default: 0 },
		{
			fieldname: "refunds",
			label: __("Refunds"),
			fieldtype: "Select",
			options: "Separate\nNet with sales\nLeave out",
			default: "Separate",
		},
	],
};
