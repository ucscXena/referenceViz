export default function parseURL(href) {
	const url = new URL(href);

	const path = url.pathname;

	// Get params as a plain object for simplicity
	const params = {};
	url.searchParams.forEach((value, key) => {
		if (params[key]) {
			// Handle duplicates by turning into an array
			if (!Array.isArray(params[key])) {
				params[key] = [params[key]];
			}
			params[key].push(value);
		} else {
			params[key] = value;
		}
	});

	return { path, params };
}

// Example usage:
//const { path, params } = parseUrl();
//console.log('Path:', path);
//console.log('Params:', params);
