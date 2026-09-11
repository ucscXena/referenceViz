
// color scale variants

import * as _ from './underscore_ext.js';
import { rgb, RGBToHex } from './color_helper.js';

// d3_category20, replace #7f7f7f gray (that aliases with our N/A gray of #808080) with dark grey #434348
var categoryMore = [
		"#1f77b4", // dark blue
//		"#17becf", // dark blue-green
		"#d62728", // dark red
		"#9467bd", // dark purple
		"#ff7f0e", // dark orange
		"#8c564b", // dark brown
		"#e377c2", // dark pink
		"#2ca02c", // dark green
		"#bcbd22", // dark mustard
//		"#434348", // very dark grey
		"#aec7e8", // light blue
//		"#9edae5", // light blue-green
		"#dbdb8d", // light mustard
		"#ff9896", // light salmon
		"#c5b0d5", // light lavender
		"#ffbb78", // light orange
		"#c49c94", // light tan
		"#f7b6d2", // light pink
		"#98df8a", // light green
//		"#c7c7c7"  // light grey
	];

var categoryMoreRgb = categoryMore.map(rgb);

var mapper = (obj, fn) => _.isArray(obj) ? obj.map(fn) : _.mapObject(obj, fn);

// Categorical (unordered) scale: distinct perceptually unrelated colors.
// d3 ordinal scales will de-dup the domain using an incredibly slow algorithm.
var category = (count, custom) => {
	// XXX why does this not handle nulls, like our other scales?
	var customRgb = custom && mapper(custom, rgb),
		fn = v => custom && custom[v] ? custom[v] :
			categoryMore[v % categoryMore.length];

	fn.rgb = v => customRgb && customRgb[v] ? customRgb[v] :
		categoryMoreRgb[v % categoryMoreRgb.length];

	return fn;
};

// Viridis control points [t, r, g, b], sampled from the canonical matplotlib LUT.
// Perceptually uniform; accessible to color-blind viewers; readable in greyscale.
var viridisPoints = [
	[0.000,  68,   1,  84],
	[0.125,  65,  68, 135],
	[0.250,  59,  82, 139],
	[0.375,  49, 104, 137],
	[0.500,  33, 145, 140],
	[0.625,  53, 183, 121],
	[0.750,  94, 201,  98],
	[0.875, 187, 215,  51],
	[1.000, 253, 231,  37],
];

var sampleViridis = t => {
	var i = 0;
	while (i < viridisPoints.length - 2 && viridisPoints[i + 1][0] <= t) { i++; }
	var [t0, r0, g0, b0] = viridisPoints[i];
	var [t1, r1, g1, b1] = viridisPoints[i + 1];
	var s = (t - t0) / (t1 - t0);
	return [Math.round(r0 + s * (r1 - r0)), Math.round(g0 + s * (g1 - g0)), Math.round(b0 + s * (b1 - b0))];
};

// Ordinal (ordered) scale: viridis from dark purple (low) to yellow (high).
var ordinal = count => {
	var rgbs = Array.from({length: count}, (_, i) => {
		var t = count <= 1 ? 0.5 : i / (count - 1);
		return sampleViridis(t);
	});
	var hexes = rgbs.map(([r, g, b]) => RGBToHex(r, g, b));
	var fn = i => hexes[i] || hexes[0];
	fn.rgb = i => rgbs[i] || rgbs[0];
	return fn;
};

// A scale for when we have no data. Implements the scale API
// so we don't have to put a bunch of special cases in the drawing code.
var noDataScale = () => "gray";
noDataScale.domain = () => [];

var colorScaleByType = {
	'no-data': () => noDataScale,
	'category': category,
	'ordinal': ordinal
};

var colorScale = ([type, ...args]) => colorScaleByType[type](...args);

var phenotypeScale = phenotype =>
	colorScale([phenotype.type || 'category',
	            (phenotype.int_to_category || []).length - 1]);

export {
	colorScale,
	phenotypeScale,
	categoryMore,
	categoryMoreRgb
};
