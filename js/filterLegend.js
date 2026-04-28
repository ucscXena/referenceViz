// singlecell legend

import legend from './legend.js';
import legendStyles from './legend.module.css';
var {item} = legendStyles;

import {conj, contains, getIn, merge, range, without} from './underscore_ext.js';
import cmpCodes from './cmpCodes';

function codedLegend({column: {filtered = [], codes, codesInView}, cmp, onClick}) {
	var data = codesInView.sort(cmp),
		labels = data.map(d => codes[d]),
		f = new Set(filtered),
		checked = data.map(d => !f.has(d));

	return legend({checked, codes: data, labels, titles: labels, onClick, max: Infinity,
		inline: true});
}

var firstMatch = (el, selector) =>
	el.matches(selector) ? el :
		el.parentElement ? firstMatch(el.parentElement, selector) :
		null;

var onCode = (state, onState, filterIndex) => ev => {
	var iStr = getIn(firstMatch(ev.target, '.' + item), ['dataset', 'code']);

	if (iStr != null) {
		var i = parseInt(iStr, 10),
			filtered = state.referenceFilters[filterIndex].filtered || [],
			next = (contains(filtered, i) ? without : conj)(filtered, i);
		onState(state => merge(state, {
			referenceFilters: state.referenceFilters.map((f, j) =>
				j === filterIndex ? {layer: f.layer, filtered: next} : f)
		}));
	}
};

export default function(state, onState, filterIndex = 0) {
	if (!state || !state.imageState || !state.referenceFilters) {
		return null;
	}
	var {imageState, referenceFilters} = state;
	var f = referenceFilters[filterIndex];
	if (!f) { return null; }
	var phenotype = getIn(imageState, ['phenotypes', f.layer]) || {};
	var codes = (phenotype.int_to_category || []).slice(1);
	var type = phenotype.type || 'category';

	return !codes.length ? null :
		codedLegend({
			onClick: onCode(state, onState, filterIndex),
			cmp: type === 'ordinal' ? (i, j) => j - i : cmpCodes(codes),
			column: {
				codes,
				codesInView: range(codes.length),
				filtered: f.filtered
			}});
}
