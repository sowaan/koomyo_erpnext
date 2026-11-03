// A Material Request made from a Koomyo purchase request can be fulfilled from a warehouse instead of a purchase order.
frappe.ui.form.on("Material Request", {
	refresh(frm) {
		if (
			frm.doc.docstatus !== 1 ||
			!frm.doc.koomyo_id ||
			frm.doc.material_request_type !== "Purchase"
		) {
			return;
		}
		frm.add_custom_button(
			__("Transfer from warehouse"),
			() => {
				frappe.prompt(
					[
						{
							fieldname: "source_warehouse",
							fieldtype: "Link",
							options: "Warehouse",
							label: __("From Warehouse"),
							reqd: 1,
							get_query: () => ({
								filters: { company: frm.doc.company, is_group: 0 },
							}),
						},
					],
					(values) => {
						frappe.call({
							method: "koomyo_erpnext.api.make_transfer_from_request",
							args: {
								material_request: frm.doc.name,
								source_warehouse: values.source_warehouse,
							},
							freeze: true,
							callback: (r) => {
								if (r.message) {
									frappe.set_route("Form", "Stock Entry", r.message);
								}
							},
						});
					},
					__("Transfer from warehouse"),
					__("Make Stock Entry")
				);
			},
			__("Create")
		);
	},
});
