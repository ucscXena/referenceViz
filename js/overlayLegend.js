// overlay legend

import legend from './legend.js';
import legendStyles from './legend.module.css';
var {item} = legendStyles;
import {span} from './react-hyper';

import {conj, contains, getIn, groupBy, mapObject, memoize1, merge, range,
    sortBy, without} from './underscore_ext.js';

var pad = (width, x) => `${width - x.toString().length}ch`;
var lengthStyle = (width, length) =>
	({fontFamily: 'monospace', marginLeft: pad(width, length), marginRight: '1ch'});

function codedLegend({column: {filtered = [], codes, lengths, codesInView}, onClick}) {
	var data = sortBy(codesInView, c => lengths[c]),
		width = lengths[data[data.length - 1]].toString().length,
		labels = data.map(d => span(span({style: lengthStyle(width, lengths[d])},
			lengths[d].toString()), codes[d])),
		titles = data.map(d => codes[d]),
		f = new Set(filtered),
		checked = data.map(d => !f.has(d));

	return legend({checked, codes: data, labels, titles, onClick, max: Infinity,
		inline: false});
}

var firstMatch = (el, selector) =>
	el.matches(selector) ? el :
		el.parentElement ? firstMatch(el.parentElement, selector) :
		null;

var onCode = (state, onState, filterIndex) => ev => {
	var iStr = getIn(firstMatch(ev.target, '.' + item), ['dataset', 'code']);

	if (iStr != null) {
		var i = parseInt(iStr, 10),
			filtered = state.overlayFilters[filterIndex].filtered || [],
			next = (contains(filtered, i) ? without : conj)(filtered, i);
		onState(state => merge(state, {
			overlayFilters: state.overlayFilters.map((f, j) =>
				j === filterIndex ? {var: f.var, filtered: next} : f)
		}));
	}
};

var groupLengths = memoize1(data => mapObject(groupBy(data, x => x), v => v.length));

// Count cells per code for filter at filterIndex, considering only cells that
// pass all other active filters.
var filteredGroupLengths = memoize1((overlay, overlayFilters, filterIndex) => {
	var {var: varName} = overlayFilters[filterIndex],
		otherFilters = overlayFilters.filter((_, j) => j !== filterIndex),
		hiddenSets = otherFilters.map(f => new Set(f.filtered)),
		codes = overlay._dicts[varName],
		counts = {};
	for (var c = 0; c < codes.length; c++) {
		counts[c] = 0;
	}
	for (var i = 0; i < overlay.x.length; i++) {
		if (hiddenSets.every((hidden, fi) => !hidden.has(overlay[otherFilters[fi].var][i]))) {
			var code = overlay[varName][i];
			counts[code] = (counts[code] || 0) + 1;
		}
	}
	return counts;
});

export default function(state, onState, filterIndex = 0) {
	if (!state || !state.overlayFilters || !state.overlayFilters[filterIndex]) {
		return null;
	}
	var {overlay, overlayFilters} = state;
	var {var: overlayVar, filtered: overlayFiltered} = overlayFilters[filterIndex];
	var codes = overlay._dicts[overlayVar],
		lengths = overlayFilters.length > 1 ?
			filteredGroupLengths(overlay, overlayFilters, filterIndex) :
			groupLengths(overlay[overlayVar]);

	return codedLegend({
			onClick: onCode(state, onState, filterIndex),
			column: {
				codes,
				lengths,
				codesInView: range(codes.length),
				filtered: overlayFiltered
			}});
}
