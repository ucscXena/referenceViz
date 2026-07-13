// singlecell legend

import legend from './legend.js';
import legendStyles from './legend.module.css';
import styles from './singlecellLegend.module.css';
var {item} = legendStyles;

import {phenotypeScale} from './colorScales';
import {Let, concat, conj, contains, getIn, memoize1, merge, uniq, without} from
	'./underscore_ext.js';
import * as gaEvents from './gaEvents';
import cmpCodes from './cmpCodes';
import {div, span} from './react-hyper';

function codedLegend({column: {scale, codes, codesInView, hidden = []}, cmp, onClick}) {
	var colorFn = scale,
		{codes: visibleCodes, counts, total} = codesInView,
		data = visibleCodes.sort(cmp),
		hiddenSet = new Set(hidden),
		highlighted = data.map(d => hiddenSet.has(d)),
		colors = data.map(colorFn),
		labels = data.map(d => codes[d]),
		percentages = total > 0 ?
			data.map(d => Let((pct = ((counts[d] || 0) / total * 100).toFixed(1)) =>
			'(' + (pct === '0.0' ? '<0.1' : pct) + '%)')) :
			data.map(() => '');

	return legend({colors, codes: data, labels, titles: labels, percentages, onClick,
		max: Infinity, inline: true, highlighted});
}

var firstMatch = (el, selector) =>
	el.matches(selector) ? el :
		el.parentElement ? firstMatch(el.parentElement, selector) :
		null;

var onCode = (state, onState, codes) => ev => {
	var iStr = getIn(firstMatch(ev.target, '.' + item), ['dataset', 'code']);

	if (iStr != null) {
		var i = parseInt(iStr, 10),
			hidden = state.hidden || [],
			isHiding = !contains(hidden, i),
			next = (isHiding ? conj : without)(hidden, i);
		gaEvents.categoryVisibility('color', isHiding ? 'hide' : 'show', codes[i] || String(i));
		onState(state => merge(state, {hidden: next}));
	}
};

var tilePoints = (viewBounds, tileSize) => viewBounds ?
	({points, index: {x: tx, y: ty, z: tz}}) =>
		Let(([minX, minY, maxX, maxY] = viewBounds,
			scale = 1 << tz,
			pxMin = minX * scale - tx * tileSize,
			pxMax = maxX * scale - tx * tileSize,
			pyMin = minY * scale - ty * tileSize,
			pyMax = maxY * scale - ty * tileSize) =>
			points.filter(([px, py]) =>
				px >= pxMin && px <= pxMax && py >= pyMin && py <= pyMax)) :
	({points}) => points;

var codesInView = memoize1((data = [], referenceFilters = [], viewBounds, tileSize) =>
	Let((hiddenSets = referenceFilters.map(f => new Set(f.filtered)),
		getPoints = tilePoints(viewBounds, tileSize),
		visible = concat(...data.map(t => t.points ? getPoints(t) : t))
			.filter(pt => hiddenSets.every((hs, i) => !hs.has(pt[3 + i]))),
		counts = visible.reduce((acc, [, , c]) => (acc[c] = (acc[c] || 0) + 1, acc), {}),
		total = visible.length) =>
		({codes: uniq(visible.map(([, , c]) => c)), counts, total})));

var cmpFreq = counts => (a, b) => (counts[a] || 0) - (counts[b] || 0);

var sortToggle = (effectiveSort, onState) =>
	div({className: styles.sortToggle},
		span('Sort: '),
		span({
			className: effectiveSort === 'freq' ? styles.sortActive : styles.sortInactive,
			onClick: () => onState(s => merge(s, {legendSort: 'freq'}))},
			'Abundance'),
		span({
			className: effectiveSort !== 'freq' ? styles.sortActive : styles.sortInactive,
			onClick: () => onState(s => merge(s, {legendSort: 'name'}))},
			'Name'));

export default function(state, onState) {
	if (!state || !state.imageState) {
		return null;
	}
	var {imageState, layer, hidden, tileData, referenceFilters = [],
		legendSort, viewBounds} = state;
	var phenotype = getIn(imageState, ['phenotypes', layer]) || {};
	var codes = (phenotype.int_to_category || []).slice(1);
	var type = phenotype.type || 'category';
	var tileSize = getIn(imageState, ['tileSize']);
	var civ = codesInView(tileData, referenceFilters, viewBounds, tileSize);
	var effectiveSort = legendSort || (type === 'ordinal' ? 'name' : 'freq');
	var cmp = effectiveSort === 'freq' ? cmpFreq(civ.counts) :
		type === 'ordinal' ? (i, j) => j - i : cmpCodes(codes);

	return !codes.length ? null :
		div(sortToggle(effectiveSort, onState),
			codedLegend({
				onClick: onCode(state, onState, codes),
				cmp,
				column: {
					codes,
					codesInView: civ,
					scale: phenotypeScale(phenotype),
					hidden
				}}));
}
