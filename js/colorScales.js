
// color scale variants

import * as _ from './underscore_ext.js';
import { rgb } from './color_helper.js';

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

//var ordinal = (count, custom) => d3.scaleOrdinal().range(custom || categoryMore).domain(_.range(count));
// d3 ordinal scales will de-dup the domain using an incredibly slow algorithm.
var ordinal = (count, custom) => {
	// XXX why does this not handle nulls, like our other scales?
	var customRgb = custom && mapper(custom, rgb),
		fn = v => custom && custom[v] ? custom[v] :
			categoryMore[v % categoryMore.length];

	fn.rgb = v => customRgb && customRgb[v] ? customRgb[v] :
		categoryMoreRgb[v % categoryMoreRgb.length];

	return fn;
};

// A scale for when we have no data. Implements the scale API
// so we don't have to put a bunch of special cases in the drawing code.
var noDataScale = () => "gray";
noDataScale.domain = () => [];

var colorScaleByType = {
	'no-data': () => noDataScale,
	'ordinal': ordinal
};

var colorScale = ([type, ...args]) => colorScaleByType[type](...args);

export {
	colorScale,
	categoryMore,
	categoryMoreRgb
};
