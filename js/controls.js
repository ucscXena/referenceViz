import singlecellLegend from './singlecellLegend';
import filterLegend from './filterLegend';
import overlayLegend from './overlayLegend';
import Tab from '@material-ui/core/Tab';
import Tabs from '@material-ui/core/Tabs';
import Typography from '@material-ui/core/Typography';
import Icon from '@material-ui/core/Icon';
import Button from '@material-ui/core/Button';
import MenuItem from '@material-ui/core/MenuItem';
import {el, div} from './react-hyper';
import PureComponent from './PureComponent';
import select from './select';
import {get, getIn, keys, Let, merge, omit, range} from './underscore_ext';
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

var filterLayerSelect = (layers, layer, onChange) =>
	select({
		id: 'filterLayer-select',
		label: 'Filter by',
		value: layer,
		onChange}, menuItem({value: -1}, 'None'),
		...layers.map((l, i) => menuItem({value: i}, l.name)));

var filterCount = state =>
	Let((codes = getIn(state,
		['imageState', 'phenotypes', state.filterLayer, 'int_to_category'], [])
			.slice(1),
		filtered = get(state, 'filtered', [])) =>
		filtered.length ? `${codes.length - filtered.length} / ${codes.length}` : '');

var overlaySelect = (vars, value, onChange) =>
	select({
		style: {minWidth: 200},
		id: 'overlay-select',
		label: 'Filter mapped data by',
		value,
		onChange}, menuItem({value: 'None'}, 'None'),
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

	onLayer = ev => {
		var layer = ev.target.value;
		this.props.onState(state => merge(state, {layer, hidden: []}));
	};

	onFilterLayer = ev => {
		var filterLayer = ev.target.value;
		this.props.onState(state => merge(state, {filterLayer, filtered: []}));
	};

	onHideAll = () => {
		var {imageState, filterLayer} = this.props.state;
		var codes = getIn(imageState, ['phenotypes', filterLayer, 'int_to_category'],
			[]).slice(1);
		this.props.onState(state => merge(state, {filtered: range(codes.length)}));
	};

	onShowAll = () => {
		this.props.onState(state => merge(state, {filtered: []}));
	};

	onOverlay = () => {
		this.props.onState(state => merge(state, {hideOverlay: !state.hideOverlay}));
	};

	onOverlayVar = ev => {
		var overlayVar = ev.target.value;
		this.props.onState(state => merge(state, {overlayVar, overlayFiltered: []}));
	};

	render() {
		var {onChange, onLayer, onFilterLayer, onHideAll, onShowAll, onOverlay,
				onOverlayVar, props: {onState, state}} = this,
			{tab: value} = this.state,
			{imageState, layer, filterLayer, overlayVar = 'None', overlay, hideOverlay} = state,
			layers = get(imageState, 'phenotypes', []),
			layerSelector = layerSelect(layers, layer, onLayer),
			filterSelector = filterLayerSelect(layers, filterLayer, onFilterLayer),
			oVars = overlayVariables(overlay),
			overlayTab = !!oVars.length;

		return (
			div(
				tabs({value, onChange, variant: 'fullWidth'},
					tab({label: 'Color'}),
					tab({label: `Filter ${filterCount(state)}`}),
					...(overlayTab ? [tab({label: 'Mapped Data'})] : [])),
				tabPanel({value, index: 0},
					overlay && !overlayTab ? overlayButton(onOverlay, !hideOverlay)
						: null,
					layerSelector,
					singlecellLegend(state, onState)),
				tabPanel({value, index: 1},
					filterSelector,
					...(filterLayer >= 0 ? [
						div(
							shButton(onHideAll, 'Hide all'),
							shButton(onShowAll, 'Show all')),
						filterLegend(state, onState)
					] : [])),
				...(overlayTab ?
					[tabPanel({value, index: 2},
						overlaySelect(oVars, overlayVar, onOverlayVar),
						overlayLegend(state, onState))]
						: [])));
	}
});
