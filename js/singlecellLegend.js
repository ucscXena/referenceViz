// singlecell legend

import legend from './legend.js';
import legendStyles from './legend.module.css';
var {item} = legendStyles;

import {phenotypeScale} from './colorScales';
import {Let, concat, conj, contains, getIn, memoize1, merge, uniq, without} from
	'./underscore_ext.js';
import cmpCodes from './cmpCodes';

function codedLegend({column: {scale, codes, codesInView, hidden = []}, cmp, onClick}) {
	var colorFn = scale,
		data = codesInView.sort(cmp),
		hiddenSet = new Set(hidden),
		highlighted = data.map(d => hiddenSet.has(d)),
		colors = data.map(colorFn),
		labels = data.map(d => codes[d]);

	return legend({colors, codes: data, labels, titles: labels, onClick, max: Infinity,
		inline: true, highlighted});
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

var codesInView = memoize1((data = [], referenceFilters = []) =>
	Let((hiddenSets = referenceFilters.map(f => new Set(f.filtered))) =>
		uniq(concat(...data)
			.filter(pt => hiddenSets.every((hs, i) => !hs.has(pt[3 + i])))
			.map(([, , c]) => c))));

export default function(state, onState) {
	if (!state || !state.imageState) {
		return null;
	}
	var {imageState, layer, hidden, tileData, referenceFilters = []} = state;
	var phenotype = getIn(imageState, ['phenotypes', layer]) || {};
	var codes = (phenotype.int_to_category || []).slice(1);
	var type = phenotype.type || 'category';

	return !codes.length ? null :
		codedLegend({
			onClick: onCode(state, onState),
			cmp: type === 'ordinal' ? (i, j) => j - i : cmpCodes(codes),
			column: {
				codes,
				codesInView: codesInView(tileData, referenceFilters),
				scale: phenotypeScale(phenotype),
				hidden
			}});
}
