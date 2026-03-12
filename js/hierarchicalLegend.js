import PureComponent from './PureComponent';
import {el, div} from './react-hyper';
import Typography from '@material-ui/core/Typography';
import Icon from '@material-ui/core/Icon';
import taxonomy from '../hierarchicalLabelTaxonomy.json';
import {colorScale} from './colorScales';
import hierarchicalColors from './hierarchicalColors';
import singlecellLegend from './singlecellLegend';
import {Let, concat, conj, contains, getIn, merge, memoize1, uniq, without} from
	'./underscore_ext';
import legendStyles from './legend.module.css';

var typography = el(Typography);
var icon = el(Icon);

var buildLookup = memoize1(codes => {
	var result = {};
	codes.forEach((label, i) => { result[label] = i; });
	return result;
});

// Returns array of [groupName, codesArray] pairs, with an 'Other' group
// appended for any codes not found in the taxonomy.
var computeGroups = memoize1((groups, codes) =>
	Let((groupNames = Object.keys(groups),
	     lookup = buildLookup(codes),
	     named = groupNames.map(g =>
	         [g, groups[g].map(l => lookup[l]).filter(c => c != null)]),
	     grouped = new Set(named.reduce((acc, [, gc]) => acc.concat(gc), [])),
	     other = codes.map((_, i) => i).filter(c => !grouped.has(c))) =>
	    other.length ? [...named, ['Other', other]] : named));

var codesInView = memoize1((data = [], filtered = []) =>
	Let((fs = new Set(filtered)) =>
	    new Set(uniq(concat(...data)
	        .filter(([, , , f]) => !fs.has(f))
	        .map(([, , c]) => c)))));

var computeColors = memoize1(hierarchicalColors);

// Looks up the taxonomy groups for the current layer by matching the layer's
// display name against the top-level keys of the taxonomy JSON.
// Returns the groups object, or null if no match.
var getTaxonomyGroups = (imageState, layer) =>
	taxonomy[getIn(imageState, ['phenotypes', layer, 'name'])] || null;

// ------------------------------------------------------------------
// HierarchicalLegend: pure UI component.
// Renders the two-level expandable legend for a given groups object.
// ------------------------------------------------------------------
class HierarchicalLegend extends PureComponent {
	state = {expanded: {}};

	onToggleExpand = (groupName, ev) => {
		ev.stopPropagation();
		this.setState(({expanded}) =>
			({expanded: merge(expanded, {[groupName]: !expanded[groupName]})}));
	};

	onGroupClick = (groupName, gc) => {
		var {state: {hidden = []}, onState} = this.props,
			allHidden = gc.every(c => contains(hidden, c)),
			gcSet = new Set(gc),
			next = allHidden
				? hidden.filter(c => !gcSet.has(c))
				: [...new Set([...hidden, ...gc])];
		onState(s => merge(s, {hidden: next}));
	};

	onItemClick = code => {
		var {state: {hidden = []}, onState} = this.props,
			next = (contains(hidden, code) ? without : conj)(hidden, code);
		onState(s => merge(s, {hidden: next}));
	};

	render() {
		var {state: {imageState, layer, customColor, hidden = [], tileData, filtered},
				groups} = this.props,
			{expanded} = this.state,
			codes = getIn(imageState, ['phenotypes', layer, 'int_to_category'], []).slice(1),
			colorFn = colorScale(['ordinal', codes.length, customColor]),
			groupList = computeGroups(groups, codes),
			inView = codesInView(tileData, filtered);

		return div({className: legendStyles.column},
			groupList
				.map(([groupName, gc]) => [groupName, gc.filter(c => inView.has(c))])
				.filter(([, gc]) => gc.length > 0)
				.map(([groupName, gc]) =>
					Let((allHidden = gc.every(c => contains(hidden, c)),
					     isExpanded = !!expanded[groupName],
					     displayName = groupName.replace(/_/g, ' ')) =>
					    div({key: groupName},
					        div({style: {display: 'flex', alignItems: 'center',
					                     cursor: 'pointer', overflow: 'hidden'},
					                onClick: () => this.onGroupClick(groupName, gc)},
					            div({style: {width: 20, height: 23, flexShrink: 0,
					                         display: 'flex', alignItems: 'center',
					                         justifyContent: 'center'},
					                    onClick: ev => this.onToggleExpand(groupName, ev)},
					                icon({style: {fontSize: 16}},
					                    isExpanded ? 'expand_more' : 'chevron_right')),
					            div({style: {width: 15, height: 15, flexShrink: 0,
					                         border: '1px solid black', margin: 4,
					                         backgroundColor:
					                             allHidden ? '#000000' : colorFn(gc[0])}},
					                null),
					            typography({component: 'label', variant: 'body1',
					                    style: {fontWeight: 'bold', cursor: 'pointer',
					                            overflow: 'hidden', textOverflow: 'ellipsis',
					                            whiteSpace: 'nowrap', paddingTop: 4}},
					                displayName)),
					        isExpanded ?
					            div({style: {paddingLeft: 20}},
					                gc.map(code =>
					                    Let((isHidden = contains(hidden, code)) =>
					                        div({key: code, className: legendStyles.item,
					                                style: {cursor: 'pointer'},
					                                onClick: () => this.onItemClick(code)},
					                            div({className: legendStyles.colorBox,
					                                    style: {backgroundColor:
					                                        isHidden ? '#000000' : colorFn(code)}},
					                                null),
					                            typography({component: 'label',
					                                    className: legendStyles.label,
					                                    variant: 'caption',
					                                    style: {cursor: 'pointer', fontSize: '0.875rem'}},
					                                codes[code]))))) : null))));
	}
}

var hierarchicalLegendEl = el(HierarchicalLegend);

// ------------------------------------------------------------------
// LegendWrapper: manages color sync and decides which legend to show.
// ------------------------------------------------------------------
class LegendWrapper extends PureComponent {
	getCodes() {
		var {state: {imageState, layer}} = this.props;
		return getIn(imageState, ['phenotypes', layer, 'int_to_category'], []).slice(1);
	}

	syncColors() {
		var {state: {imageState, layer}, onState} = this.props;
		if (!imageState) { return; }
		var groups = getTaxonomyGroups(imageState, layer),
			customColor = groups ?
				computeColors(groups, this.getCodes()) :
				undefined;
		onState(s => merge(s, {customColor}));
	}

	componentDidMount() {
		this.syncColors();
	}

	componentDidUpdate(prevProps) {
		var {state: {imageState, layer}} = this.props,
			{state: {imageState: prevImageState, layer: prevLayer}} = prevProps;
		if (imageState !== prevImageState || layer !== prevLayer) {
			this.syncColors();
		}
	}

	render() {
		var {state, onState} = this.props,
			groups = getTaxonomyGroups(state.imageState, state.layer);
		return groups ?
			hierarchicalLegendEl({state, onState, groups}) :
			singlecellLegend(state, onState);
	}
}

var legendWrapper = el(LegendWrapper);

export default (state, onState) =>
	state && state.imageState ?
		legendWrapper({state, onState}) :
		null;
