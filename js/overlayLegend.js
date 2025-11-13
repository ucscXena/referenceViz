// overlay legend

import legend from './legend.js';
import legendStyles from './legend.module.css';
var {item} = legendStyles;

import {conj, contains, getIn, merge, range, without} from './underscore_ext.js';
import cmpCodes from './cmpCodes';

function codedLegend({column: {filtered = [], codes, codesInView}, onClick}) {
	var data = codesInView.sort(cmpCodes(codes)),
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

var onCode = (state, onState) => ev => {
	var iStr = getIn(firstMatch(ev.target, '.' + item), ['dataset', 'code']);

	if (iStr != null) {
		var i = parseInt(iStr, 10),
			filtered = state.overlayFiltered || [],
			next = (contains(filtered, i) ? without : conj)(filtered, i);
		onState(state => merge(state, {overlayFiltered: next}));
	}
};

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
				codesInView: range(codes.length),
				filtered: overlayFiltered
			}});
}
