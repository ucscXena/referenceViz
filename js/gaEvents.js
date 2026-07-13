// GA4 event helpers for the viz app.
// Silent no-ops when gtag is not loaded (dev environment, or before async load).

var send = (name, params) => {
	if (window.gtag) {
		window.gtag('event', name, params);
	}
};

export var colorByChange = value =>
	send('color_by_change', {value});

export var filterByChange = value =>
	send('filter_by_change', {value});

export var mappedDataChange = value =>
	send('mapped_data_change', {value});

export var refineByChange = (tab, value) =>
	send('refine_by_change', {tab, value});

export var visibilityBulk = (tab, action) =>
	send('visibility_bulk', {tab, action});

export var categoryVisibility = (tab, action, category) =>
	send('visibility_category', {tab, action, category});

export var dotSizeChange = (target, value) =>
	send('dot_size_change', {target, value});
