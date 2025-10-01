import {Icon, IconButton, MenuItem, Slider} from '@material-ui/core';
import PureComponent from './PureComponent';
import styles from './singlecellView.module.css';
import {div, el, img, label, span} from './react-hyper.js';
import {get, getIn, identity, indexOf, Let, memoize1, merge, omit,
	pick} from './underscore_ext.js';
import spinner from './ajax-loader.gif';
import tiledScatterplot from './tiledScatterplot';
import '../fonts/index.css';
import Rx from './rx';
import colorPicker from './colorPicker';
import select from './select';
var {ajax} = Rx.Observable;

// XXX currently ignoring radiusBase param
var dotRange = () => Let((min = 1, max = 100) =>
	({min, max, step: (max - min) / 200}));

var iconButton = el(IconButton);
var icon = el(Icon);
var slider = el(Slider);
var menuItem = el(MenuItem);

// Styles

// https://gamedev.stackexchange.com/questions/53601/why-is-90-horz-60-vert-the-default-fps-field-of-view
//var perspective = 60;

var id = (...arr) => arr.filter(identity);

var getStatusView = el(({loading, error, onReload}) =>
	loading ? div({className: styles.status},
				img({style: {textAlign: 'center'}, src: spinner})) :
		// XXX this is broken
	error ? div({className: styles.status},
				iconButton({
						onClick: onReload,
						title: 'Error loading data. Click to reload.',
						ariaHidden: 'true'},
					icon('warning'))) :
	null);

var scale = um =>
	div({className: styles.scale},
		span(), span(), span(), span(`${um == null ? '-' : um.toFixed()} \u03BCm`));

var tooltipValueView = (value, onClick) =>
	div({className: styles.tooltip},
	    span(value, icon({onClick}, 'close')));

var labelFormat = v => v.toPrecision(2);
var dotSize = ({state, onRadius: onChange}) =>
	!state.radiusBase ? null :
	div(label('Dot size'),
		slider({...dotRange(state.radiusBase),
			valueLabelDisplay: 'auto',
			valueLabelFormat: labelFormat,
			marks: [{value: state.radiusBase,
				label: state.radiusBase.toPrecision(2)}],
				value: state.radius, onChange}));

var s = (...args) => id(...args).join(' ');

var controlsView = ({state, showControls, onControls, onRadius}) =>
	Let((controls = id(dotSize({state, onRadius}))) =>
		div({className: s(styles.controls,
			              showControls && controls.length && styles.open)},
			...(showControls ? controls : []),
			...(controls.length ? [icon({onClick: onControls}, 'settings')] : [])));

var getImageMeta =  path => ajax({
		url: `${path}/metadata.json`,
		withCredentials: true,
		headers: {'X-Redirect-To': location.origin},
		responseType: 'text', method: 'GET', crossDomain: true
	}).map(r => JSON.parse(r.response));

var layerSelect = (layers, layer, onChange) =>
	select({
		id: 'layer-select',
		value: layer,
		onChange}, ...layers.map((l, i) => menuItem({value: i}, l.name)));

export default el(class SinglecellView extends PureComponent {
	state = {
		tooltipID: undefined,
		tooltipValue: undefined,
		scale: null,
		showControls: false,
		radius: 10
	};
	//	For displaying FPS
	componentDidMount() {
		getImageMeta(this.props.image).subscribe(
			imageState => {
				this.setState({imageState}); // XXX why local and upstream?
				this.props.onState(state => merge(state, {imageState}));
			},
			() => this.setState({error: true})
		);
		//		this.timer = setInterval(() => {
		//			if (this.FPSRef && this.deckGL) {
		//				this.FPSRef.innerHTML = `${this.deckGL.deck.metrics.fps.toFixed(0)} FPS`;
		//			}
		//		}, 1000);
	}
	//	componentWillUnmount() {
	//		clearTimeout(this.timer);
	//	}
	onFPSRef = FPSRef => {
		this.FPSRef = FPSRef;
	};
	onDeck = deckGL => {
		this.deckGL = deckGL;
	};
	onRef = ref => {
		if (ref) {
			this.setState({container: ref});
		}
	};
	// XXX what is upp, why is it here?
	onViewState = (viewState, upp) => {
		var unit = getIn(this.props.state, ['dataset', 'micrometer_per_unit']);
		if (upp && unit) {
			this.setState({scale: 100 * upp * unit});
		} else {
			this.setState({scale: null});
		}
		if (viewState) {
			this.props.onState(state =>
				merge(state, {viewState:
					omit(viewState, 'transitionDuration', 'transitionInterpolator')}));
		}
	};
	findSample = memoize1((samples, id) => indexOf(samples, id, true));
	onTooltip = i => {
		this.setState({tooltipValue: i, tooltipID: undefined});
	};
	onClose = () => {
		this.setState({tooltipID: undefined, tooltipValue: undefined});
	};
	onControls = () => {
		this.setState({showControls: !this.state.showControls});
	};
	onRadius = (ev, radius) => {
		// XXX add handle for click on label. See Map.js
		this.setState({radius});
	};
	onLayer = ev => {
		var layer = ev.target.value;
		this.props.onState(state => merge(state, {layer}));
	};
	render() {
		var handlers = pick(this.props, (v, k) => k.startsWith('on'));

		var {onViewState, onTooltip, onClose, onControls, onDeck, onLayer, onRadius,
			onReload} = this,
			{image, state, onState, onShadow} = this.props,
			{hidden, layer} = state || {},
			error = this.state.error,
			unit = false,
			{container, tooltipValue, showControls, imageState, radius,
				viewState} = this.state,
			loading = !imageState,
			count = get(imageState, 'count'),
			//			name = getIn(imageState, ['phenotypes', layer, 'name']),
			layers = get(imageState, 'phenotypes', []),
			layerSelector = layerSelect(layers, layer, onLayer);

		return div({className: styles.content},
			div({className: styles.title},
				layerSelector,
				//					name ? span(name) : '',
				count ? span(`${count.toLocaleString()} cells`) : ''),
			span({className: styles.fps, ref: this.onFPSRef}),
			div({className: styles.graphWrapper, ref: this.onRef},
				controlsView({state: {radiusBase: 10, radius}, showControls, onControls,
					onRadius, onShadow}),
				get(state, 'showColorPicker') ? colorPicker({state, onState, layer}) :
					null,
				...(unit ? [scale(this.state.scale)] : []),
				...(tooltipValue ? [tooltipValueView(tooltipValue, onClose)] : []),
				getStatusView({loading, error, onReload, key: 'status'}),
				tiledScatterplot({...handlers, onViewState, onDeck,
					onTooltip, radius, viewState, hidden, image: {path: image,
						'image_scalef': 1}, imageState, layer, container, // XXX scalef
					key: 'drawing'})));
	}
});
