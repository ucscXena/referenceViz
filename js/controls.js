import singlecellLegend from './singlecellLegend';
import filterLegend from './filterLegend';
import overlayLegend from './overlayLegend';
import * as gaEvents from './gaEvents';
import Tab from '@material-ui/core/Tab';
import Tabs from '@material-ui/core/Tabs';
import Typography from '@material-ui/core/Typography';
import Icon from '@material-ui/core/Icon';
import Button from '@material-ui/core/Button';
import MenuItem from '@material-ui/core/MenuItem';
import {el, div} from './react-hyper';
import PureComponent from './PureComponent';
import select from './select';
import {get, getIn, keys, merge, omit, range} from './underscore_ext';
import legendStyles from './legend.module.css';

var button = el(Button);
var tab = el(Tab);
var tabs = el(Tabs);
var menuItem = el(MenuItem);
var typography = el(Typography);
var icon = el(Icon);

// The 'transform' is a workaround for a chrome bug that causes
// a scrolled element to be rendered at the wrong scroll position.
var tabStyle = {overflowY: 'auto', overflowX: 'hidden', flex: 1,
	transform: 'translateZ(0)'};
var tabPanel = ({value, index}, ...children) =>
	div({hidden: value !== index, style: tabStyle}, ...children);

var layerSelect = (layers, layer, onChange) =>
	select({
		id: 'layer-select',
		label: 'Color by',
		value: layer,
		onChange}, ...layers.map((l, i) => menuItem({value: i}, l.name)));

var isFiltered = state =>
	getIn(state, ['referenceFilters'], []).some(f => f.filtered.length > 0);

var isMappedFiltered = state =>
	getIn(state, ['overlayFilters'], []).some(f => f.filtered.length > 0);

var overlaySelect = (vars, value, onChange, label) =>
	select({
		style: {minWidth: 200},
		id: 'overlay-select',
		label: label || 'Filter mapped data by',
		value,
		onChange},
		...vars.map(l => menuItem({value: l}, l)));

var shButtonStyle = {
	fontSize: '70%',
	marginRight: 2,
	minWidth: 'unset',
	height: 30
};

var shButton = (onClick, txt) =>
	button({style: shButtonStyle, onClick,
		variant: 'outlined', size: 'small'}, txt);

var overlayButton = (onClick, checked) =>
	div({className: legendStyles.item, onClick},
		div({className: legendStyles.colorBox, style: {backgroundColor: 'ffffff'}},
			checked ? icon('checked') : null),
		typography({component: 'label', className: legendStyles.label},
			'Mapped data'));

var overlayVariables = overlay => keys(omit(overlay, 'x', 'y', '_dicts'));


export default el(class extends PureComponent {
	state = {tab: 0};
	onChange = (ev, value) => {
		this.setState({tab: value});
	};

	componentDidUpdate(prevProps) {
		var wasOverlay = !!overlayVariables(prevProps.state.overlay).length,
			isOverlay = !!overlayVariables(this.props.state.overlay).length;
		if (!wasOverlay && isOverlay) {
			this.setState({tab: 2});
		}
	}

	onLayer = ev => {
		var layer = ev.target.value;
		var name = getIn(this.props.state, ['imageState', 'phenotypes', layer, 'name']);
		gaEvents.colorByChange(name || String(layer));
		this.props.onState(state => merge(state, {layer, hidden: [], legendSort: null}));
	};

	onRefFilterVar = (i, value) => {
		var name = value >= 0 ?
			getIn(this.props.state, ['imageState', 'phenotypes', value, 'name']) || String(value) :
			'None';
		if (i === 0) { gaEvents.filterByChange(name); }
		else { gaEvents.refineByChange('filter', name); }
		if (i === 0 && value === -1) {
			this.props.onState(state => merge(state, {referenceFilters: []}));
		} else {
			this.props.onState(state => merge(state, {
				referenceFilters: state.referenceFilters.map((f, j) =>
					j === i ? {layer: value, filtered: []} : f)
			}));
		}
	};

	onRefHideAll = i => {
		gaEvents.visibilityBulk('filter', 'hide_all');
		var {imageState, referenceFilters} = this.props.state;
		var codes = getIn(imageState,
			['phenotypes', referenceFilters[i].layer, 'int_to_category'], []).slice(1);
		this.props.onState(state => merge(state, {
			referenceFilters: state.referenceFilters.map((f, j) =>
				j === i ? {layer: f.layer, filtered: range(codes.length)} : f)
		}));
	};

	onRefShowAll = i => {
		gaEvents.visibilityBulk('filter', 'show_all');
		this.props.onState(state => merge(state, {
			referenceFilters: state.referenceFilters.map((f, j) =>
				j === i ? {layer: f.layer, filtered: []} : f)
		}));
	};

	onAddRefRefinement = () => {
		var {imageState, referenceFilters} = this.props.state;
		var layers = get(imageState, 'phenotypes', []);
		var usedLayers = new Set(referenceFilters.map(f => f.layer));
		var nextLayer = layers.findIndex((_, i) => !usedLayers.has(i));
		if (nextLayer >= 0) {
			this.props.onState(state => merge(state, {
				referenceFilters: [...state.referenceFilters, {layer: nextLayer, filtered: []}]
			}));
		}
	};

	onRemoveRefRefinement = i => {
		this.props.onState(state => merge(state, {
			referenceFilters: state.referenceFilters.filter((_, j) => j !== i)
		}));
	};

	onOverlay = () => {
		this.props.onState(state => merge(state, {hideOverlay: !state.hideOverlay}));
	};

	onOverlayVar = (i, value) => {
		if (i === 0) { gaEvents.mappedDataChange(value); }
		else { gaEvents.refineByChange('mapped', value); }
		this.props.onState(state => merge(state, {
			overlayFilters: state.overlayFilters.map((f, j) =>
				j === i ? {var: value, filtered: []} : f)
		}));
	};

	onOverlayHideAll = i => {
		gaEvents.visibilityBulk('mapped', 'hide_all');
		var {overlay, overlayFilters} = this.props.state;
		var codes = overlay._dicts[overlayFilters[i].var];
		this.props.onState(state => merge(state, {
			overlayFilters: state.overlayFilters.map((f, j) =>
				j === i ? {var: f.var, filtered: range(codes.length)} : f)
		}));
	};

	onOverlayShowAll = i => {
		gaEvents.visibilityBulk('mapped', 'show_all');
		this.props.onState(state => merge(state, {
			overlayFilters: state.overlayFilters.map((f, j) =>
				j === i ? {var: f.var, filtered: []} : f)
		}));
	};

	onAddRefinement = () => {
		var {overlay, overlayFilters} = this.props.state;
		var oVars = overlayVariables(overlay);
		var usedVars = new Set(overlayFilters.map(f => f.var));
		var nextVar = oVars.find(v => !usedVars.has(v)) || oVars[0];
		this.props.onState(state => merge(state, {
			overlayFilters: [...state.overlayFilters, {var: nextVar, filtered: []}]
		}));
	};

	onRemoveRefinement = i => {
		this.props.onState(state => merge(state, {
			overlayFilters: state.overlayFilters.filter((_, j) => j !== i)
		}));
	};

	render() {
		var {onChange, onLayer, onOverlay,
				props: {onState, state}} = this,
			{tab: value} = this.state,
			{imageState, layer, referenceFilters = [], overlayFilters = [], overlay,
				hideOverlay, overlayTitle} = state,
			layers = get(imageState, 'phenotypes', []),
			layerSelector = layerSelect(layers, layer, onLayer),
			oVars = overlayVariables(overlay),
			overlayTab = !!oVars.length;

		var refFilterRow = i => {
			var f = referenceFilters[i],
				usedLayers = new Set(referenceFilters.map((g, j) => j !== i ? g.layer : null)),
				availableLayers = layers
					.map((l, idx) => idx)
					.filter(idx => !usedLayers.has(idx) || idx === f.layer),
				label = i === 0 ? 'Filter by' : 'Refine by';
			return [
				div({style: {display: 'flex', alignItems: 'center'}},
					select({
						id: `ref-filter-select-${i}`,
						label,
						value: f.layer,
						onChange: ev => this.onRefFilterVar(i, ev.target.value)},
						...(i === 0 ? [menuItem({value: -1}, 'None')] : []),
						...availableLayers.map(idx => menuItem({value: idx}, layers[idx].name))),
					i > 0 ? button({
						style: {minWidth: 'unset', padding: 4},
						onClick: () => this.onRemoveRefRefinement(i),
						size: 'small'}, icon('close')) : null),
				div(
					shButton(() => this.onRefHideAll(i), 'Hide all'),
					shButton(() => this.onRefShowAll(i), 'Show all')),
				filterLegend(state, onState, i)
			];
		};

		var filterRow = (i) => {
			var f = overlayFilters[i],
				usedVars = new Set(overlayFilters.map((g, j) => j !== i ? g.var : null)),
				availableVars = oVars.filter(v => !usedVars.has(v) || v === f.var),
				label = i === 0 ?
					(overlayTitle ? `Filter mapped data by ${overlayTitle}` : 'Filter mapped data by') :
					'Refine by';
			return [
				div({style: {display: 'flex', alignItems: 'center'}},
					overlaySelect(availableVars, f.var,
						ev => this.onOverlayVar(i, ev.target.value), label),
					i > 0 ? button({
						style: {minWidth: 'unset', padding: 4},
						onClick: () => this.onRemoveRefinement(i),
						size: 'small'}, icon('close')) : null),
				div(
					shButton(() => this.onOverlayHideAll(i), 'Hide all'),
					shButton(() => this.onOverlayShowAll(i), 'Show all')),
				overlayLegend(state, onState, i)
			];
		};

		return (
			div(
				tabs({value, onChange, variant: 'fullWidth'},
					tab({label: 'Color'}),
					tab({label: isFiltered(state) ? 'Filter \u25cf' : 'Filter'}),
					...(overlayTab ? [tab({label: isMappedFiltered(state) ? 'Mapped Data \u25cf' : 'Mapped Data'})] : [])),
				tabPanel({value, index: 0},
					overlay && !overlayTab ? overlayButton(onOverlay, !hideOverlay)
						: null,
					layerSelector,
					singlecellLegend(state, onState)),
				tabPanel({value, index: 1},
					referenceFilters.length === 0 ?
						select({
							id: 'ref-filter-select-0',
							label: 'Filter by',
							value: -1,
							onChange: ev => {
								var layer = ev.target.value;
								if (layer >= 0) {
									var name = getIn(imageState, ['phenotypes', layer, 'name']);
									gaEvents.filterByChange(name || String(layer));
									this.props.onState(state => merge(state,
										{referenceFilters: [{layer, filtered: []}]}));
								}
							}},
							menuItem({value: -1}, 'None'),
							...layers.map((l, i) => menuItem({value: i}, l.name))) :
						div(
							...referenceFilters.flatMap((_, i) => refFilterRow(i)),
							referenceFilters.length < 3 &&
								layers.some((_, i) => !new Set(referenceFilters.map(f => f.layer)).has(i)) ?
								shButton(this.onAddRefRefinement, 'Refine by') : null)),
				...(overlayTab ?
					[tabPanel({value, index: 2},
						...overlayFilters.flatMap((_, i) => filterRow(i)),
						overlayFilters.length > 0 && overlayFilters.length < 3 &&
							oVars.some(v => !new Set(overlayFilters.map(f => f.var)).has(v)) ?
							shButton(this.onAddRefinement, 'Refine by') : null
					)] : [])));
	}
});
