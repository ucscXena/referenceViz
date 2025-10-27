import singlecellLegend from './singlecellLegend';
import filterLegend from './filterLegend';
import Tab from '@material-ui/core/Tab';
import Tabs from '@material-ui/core/Tabs';
import MenuItem from '@material-ui/core/MenuItem';
import {el, div} from './react-hyper';
import PureComponent from './PureComponent';
import select from './select';
import {merge, get} from './underscore_ext';

var tab = el(Tab);
var tabs = el(Tabs);
var menuItem = el(MenuItem);

var tabPanel = ({value, index}, ...children) =>
	div({hidden: value !== index}, ...children);

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

export default el(class extends PureComponent {
	state = {tab: 0};
	onChange = (ev, value) => {
		this.setState({tab: value});
	};

	onLayer = ev => {
		var layer = ev.target.value;
		this.props.onState(state => merge(state, {layer}));
	};

	onFilterLayer = ev => {
		var filterLayer = ev.target.value;
		this.props.onState(state => merge(state, {filterLayer}));
	};

	render() {
		var {onChange, onLayer, onFilterLayer, props: {onState, state}} = this,
			{tab: value} = this.state,
			{imageState, layer, filterLayer} = state,
			layers = get(imageState, 'phenotypes', []),
			layerSelector = layerSelect(layers, layer, onLayer),
			filterSelector = filterLayerSelect(layers, filterLayer, onFilterLayer);


		return (
			div(
				tabs({value, onChange, variant: 'fullWidth'},
					tab({label: 'Color'}),
					tab({label: 'Filter'})),
				tabPanel({value, index: 0},
					layerSelector,
					singlecellLegend(state, onState)),
				tabPanel({value, index: 1},
					filterSelector,
					filterLayer >= 0 ?
						filterLegend(state, onState) : null)));
	}
});
