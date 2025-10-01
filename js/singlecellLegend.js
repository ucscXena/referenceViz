// singlecell legend

import legend from './legend.js';
import legendStyles from './legend.module.css';
var {item} = legendStyles;

import {colorScale} from './colorScales';
import {conj, contains, getIn, merge, range, without} from './underscore_ext.js';
import cmpCodes from './cmpCodes';
import setScale from './setScale';

function codedLegend({column: {color, codes, codesInView}, onClick}) {
	var colorFn = colorScale(color),
		data = codesInView.sort(cmpCodes(codes)),
		colors = data.map(colorFn),
		labels = data.map(d => codes[d]);

	return legend({colors, codes: data, labels, titles: labels, onClick, max: Infinity,
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
			hidden = state.hidden || [],
			next = (contains(hidden, i) ? without : conj)(hidden, i);
		onState(state => merge(state, {hidden: next}));
	}
};

export default function(state, onState) {
	if (!state || !state.imageState) {
		return null;
	}
	var {imageState, layer, customColor, hidden} = state;
	var codes = getIn(imageState, ['phenotypes', layer, 'int_to_category'], [])
		.slice(1);

	return !codes.length ? null :
		codedLegend({
			onClick: onCode(state, onState),
			column: {
				codes,
				codesInView: range(codes.length),
				color: setScale(['ordinal', codes.length, customColor], hidden)
			}});
}
