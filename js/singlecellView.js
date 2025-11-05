import Icon from '@material-ui/core/Icon';
import IconButton from '@material-ui/core/IconButton';
import Slider from '@material-ui/core/Slider';
import PureComponent from './PureComponent';
import styles from './singlecellView.module.css';
import {div, el, img, label, span} from './react-hyper.js';
import {get, getIn, identity, indexOf, Let, memoize1, merge, object, omit,
	pick, pluck} from './underscore_ext.js';
import spinner from './ajax-loader.gif';
import tiledScatterplot from './tiledScatterplot';
import '../fonts/index.css';
import Rx from './rx';
import colorPicker from './colorPicker';
import {colorScale} from './colorScales';
import setScale from './setScale';
import legendStyles from './legend.module.css';
import {tableFromIPC} from 'apache-arrow';
var {ajax} = Rx.Observable;

// XXX currently ignoring radiusBase param
var dotRange = () => Let((min = 0.5, max = 10) =>
	({min, max, step: (max - min) / 200}));

var iconButton = el(IconButton);
var icon = el(Icon);
var slider = el(Slider);

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

var tooltipValueView = (code, color, onClick) =>
	div({className: styles.tooltip},
		div({className: legendStyles.colorBox,
			style: {backgroundColor: color}}),
		code,
		icon({onClick}, 'close')
	);

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
		responseType: 'text', method: 'GET', crossDomain: true
	}).map(r => JSON.parse(r.response));

var getOverlay = path => ajax({
		url: `${path}`,
		responseType: 'arraybuffer', method: 'GET', crossDomain: true
	}).map(r => r.response);

function forceRedraw(deck) {
	if (deck) {
		deck.deck.setProps({}); // triggers re-render
		deck.deck.redraw(true); // explicit redraw
	}
}

export default el(class SinglecellView extends PureComponent {
	state = {
		tooltipID: undefined,
		tooltipValue: undefined,
		scale: null,
		showControls: false,
		radius: 2
	};
	//	For displaying FPS
	componentDidMount() {
		getImageMeta(this.props.image).subscribe(
			imageState => {
				this.props.onState(state => merge(state, {imageState}));
			},
			() => this.setState({error: true})
		);
		this.props.overlay &&
			getOverlay(this.props.overlay).subscribe(
				ipc => {
					var table = tableFromIPC(ipc);
					var names = pluck(table.schema.fields, 'name');
					var data = pluck(table.batches[0].data.children, 'values');
					var overlay = object(names, data);
					this.props.onState(state => merge(state, {overlay}));
				},
				() => this.setState({error: true}));
		this.intervalId =  Let((lastPixelRatio = window.devicePixelRatio) =>
			setInterval(() => {
			  if (window.devicePixelRatio !== lastPixelRatio) {
				lastPixelRatio = window.devicePixelRatio;
				forceRedraw(this.deckGL);
			  }
			}, 500));
		//		this.timer = setInterval(() => {
		//			if (this.FPSRef && this.deckGL) {
		//				this.FPSRef.innerHTML = `${this.deckGL.deck.metrics.fps.toFixed(0)} FPS`;
		//			}
		//		}, 1000);
	}
	componentWillUnmount() {
//		clearTimeout(this.timer);
		clearInterval(this.intervalId);
	}
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
	getScale = memoize1((codes, hidden) =>
		colorScale(setScale(['ordinal', codes.length], hidden)));
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
	onTileData = tileData => {
		this.props.onState(state => merge(state, {tileData}));
	};

	render() {
		var handlers = pick(this.props, (v, k) => k.startsWith('on'));

		var {onViewState, onTooltip, onClose, onControls, onDeck, /*onLayer, */onRadius,
			onReload, onTileData} = this,
			{image, state, onState, onShadow} = this.props,
			{hidden, filtered, layer, filterLayer, imageState, overlay,
				hideOverlay} = state || {},
			error = this.state.error,
			unit = false,
			{container, tooltipValue, showControls, radius,
				viewState} = this.state,
			loading = !imageState,
			codes = getIn(imageState, ['phenotypes', layer, 'int_to_category'], [])
				.slice(1),
			tooltipColor = this.getScale(codes, hidden)(tooltipValue),
			count = get(imageState, 'count'),
			name = get(imageState, 'reference_name');

		return div({className: styles.content},
			div({className: styles.title},
				name ? span(name) : '',
				span({className: styles.spacer}),
				count ? span(`${count.toLocaleString()} cells`) : ''),
			span({className: styles.fps, ref: this.onFPSRef}),
			div({className: styles.graphWrapper, ref: this.onRef},
				controlsView({state: {radiusBase: 10, radius}, showControls, onControls,
					onRadius, onShadow}),
				get(state, 'showColorPicker') ? colorPicker({state, onState, layer}) :
					null,
				...(unit ? [scale(this.state.scale)] : []),
				...(tooltipValue != null ?
					[tooltipValueView(codes[tooltipValue], tooltipColor, onClose)]
					: []),
				getStatusView({loading, error, onReload, key: 'status'}),
				tiledScatterplot({...handlers, onViewState, onDeck, onTileData,
					onTooltip, radius, viewState, hidden, filtered, image,
					imageState, overlay, hideOverlay, layer, filterLayer, container,
					key: 'drawing'})));
	}
});
