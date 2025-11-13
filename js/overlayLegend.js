// overlay legend

import legend from './legend.js';
import legendStyles from './legend.module.css';
var {item} = legendStyles;

import {conj, contains, getIn, groupBy, mapObject, memoize1, merge, range,
    sortBy, without} from './underscore_ext.js';

function codedLegend({column: {filtered = [], codes, lengths, codesInView}, onClick}) {
	var data = sortBy(codesInView, c => lengths[c]),
		labels = data.map(d => `${codes[d]} (${lengths[d]})`),
		f = new Set(filtered),
		checked = data.map(d => !f.has(d));

	return legend({checked, codes: data, labels, titles: labels, onClick, max: Infinity,
		inline: true});
}

var firstMatch = (el, selector) =>
	el.matches(selector) ? el :
		el.parentElement ? firstMatch(el.parentElement, selector) :
		null;

var onCode = (state, onState) => ev => {
	var iStr = getIn(firstMatch(ev.target, '.' + item), ['dataset', 'code']);

	if (iStr != null) {
		var i = parseInt(iStr, 10),
			filtered = state.overlayFiltered || [],
			next = (contains(filtered, i) ? without : conj)(filtered, i);
		onState(state => merge(state, {overlayFiltered: next}));
	}
};

var groupLengths = memoize1(data => mapObject(groupBy(data, x => x), v => v.length));

export default function(state, onState) {
	if (!state || state.overlayVar === 'None') {
		return null;
	}
	var {overlay, overlayVar, overlayFiltered} = state;
	var codes = overlay._dicts[overlayVar];

	return codedLegend({
			onClick: onCode(state, onState),
			column: {
				codes,
				lengths: groupLengths(overlay[overlayVar]),
				codesInView: range(codes.length),
				filtered: overlayFiltered
			}});
}
