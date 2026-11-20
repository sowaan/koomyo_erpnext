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
			fieldname: "refunds",
			label: __("Refunds"),
			fieldtype: "Select",
			options: "Separate\nNet with sales\nLeave out",
			default: "Separate",
		},
	],
};
