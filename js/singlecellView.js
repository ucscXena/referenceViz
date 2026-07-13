import Icon from '@material-ui/core/Icon';
import IconButton from '@material-ui/core/IconButton';
import Slider from '@material-ui/core/Slider';
import PureComponent from './PureComponent';
import styles from './singlecellView.module.css';
import {div, el, img, label, span} from './react-hyper.js';
import {assoc, get, getIn, identity, indexOf, Let, memoize1, merge, object, omit,
	pick, pluck, without} from './underscore_ext.js';
import spinner from './ajax-loader.gif';
import tiledScatterplot from './tiledScatterplot';
import '../fonts/index.css';
import Rx from './rx';
import colorPicker from './colorPicker';
import {phenotypeScale} from './colorScales';
import * as gaEvents from './gaEvents';
import legendStyles from './legend.module.css';
import {tableFromIPC} from 'apache-arrow';
var {ajax} = Rx.Observable;

// XXX currently ignoring radiusBase param
// power law anchored at n=1000->3, n=100000->0.5, clamped to [0.5, 3]
var defaultOverlayRadius = n =>
	Math.min(3, Math.max(0.5, 3 * Math.pow(n / 1000, -0.389)));

var dotRange = () => Let((min = 0.5, max = 4) =>
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
var dotSlider = (labelTxt, range, value, onChange) =>
	div(label(labelTxt),
		slider({...range,
			valueLabelDisplay: 'auto',
			valueLabelFormat: labelFormat,
			value, onChange,
			onChangeCommitted: (ev, v) =>
				gaEvents.dotSizeChange(labelTxt.toLowerCase().replace(/\s+/g, '_'), v)}));

var dotSizes = ({state, onRadius, onOverlayRadius, hasOverlay}) =>
	!state.radiusBase ? null :
	div(dotSlider('Reference', dotRange(state.radiusBase), state.radius, onRadius),
		...(hasOverlay ?
			[dotSlider('Mapped data', dotRange(state.radiusBase), state.overlayRadius,
				onOverlayRadius)] :
			[]));

var s = (...args) => id(...args).join(' ');

var controlsView = ({state, showControls, onControls, onRadius, onOverlayRadius,
		hasOverlay}) =>
	Let((controls = id(dotSizes({state, onRadius, onOverlayRadius, hasOverlay}))) =>
		div({className: s(styles.controls,
			              showControls && controls.length && styles.open)},
			...(showControls ? controls : []),
			...(controls.length ? [icon({onClick: onControls}, 'settings')] : [])));

var getImageMeta =  path => ajax({
		url: `${path}/metadata.json`,
		responseType: 'text', method: 'GET', crossDomain: true
	}).map(r => JSON.parse(r.response));

var fetchOverlay = url => ajax({
		url,
		responseType: 'arraybuffer', method: 'GET', crossDomain: true
	}).map(r => r.response);

var presignOverlay = uri => ajax({
		url: `/jobs/presign/?uri=${encodeURIComponent(uri)}`,
		responseType: 'text', method: 'GET'
	}).map(r => JSON.parse(r.response));

var getOverlay = path =>
	path.startsWith('s3://') ?
		presignOverlay(path).flatMap(({url, original_filename: originalFilename}) =>
			fetchOverlay(url).map(ipc => ({ipc, originalFilename}))) :
		fetchOverlay(path).map(ipc => ({ipc}));

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
		showControls: true,
		radius: 1.5,
		overlayRadius: 3
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
				({ipc, originalFilename}) => {
					var table = tableFromIPC(ipc);
					var names = pluck(table.schema.fields, 'name');
					var dicts = table.batches[0].data.children.map(f =>
						f.dictionary && f.dictionary.toArray());
					var data = pluck(table.batches[0].data.children, 'values');
					var overlay = assoc(object(names, data), '_dicts',
						object(names, dicts));
					var overlayVars = without(names, 'x', 'y');
					var overlayFilters = overlayVars.length ?
						[{var: overlayVars[0], filtered: []}] : [];
					this.setState({overlayRadius: defaultOverlayRadius(overlay.x.length)});
					this.props.onState(state => merge(state, {overlay, overlayFilters,
						...(originalFilename ? {overlayTitle: originalFilename} : {})}));
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
			var {container} = this.state,
				{target: [cx, cy], zoom} = viewState,
				{width, height} = container ? container.getBoundingClientRect() : {},
				s = Math.pow(2, zoom),
				viewBounds = width ?
					[cx - width / (2 * s), cy - height / (2 * s),
					 cx + width / (2 * s), cy + height / (2 * s)] : null;
			this.props.onState(state =>
				merge(state, {viewState:
					omit(viewState, 'transitionDuration', 'transitionInterpolator'),
					viewBounds}));
		}
	};
	findSample = memoize1((samples, id) => indexOf(samples, id, true));
	getScale = memoize1(phenotypeScale);
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
	onOverlayRadius = (ev, overlayRadius) => {
		this.setState({overlayRadius});
	};
	onTileData = tileData => {
		this.props.onState(state => merge(state, {tileData}));
	};

	render() {
		var handlers = pick(this.props, (v, k) => k.startsWith('on'));

		var {onViewState, onTooltip, onClose, onControls, onDeck, /*onLayer, */onRadius,
			onOverlayRadius, onReload, onTileData} = this,
			{image, state, onState, onShadow, title: titleProp} = this.props,
			{hidden, referenceFilters = [], layer, imageState, overlay,
				hideOverlay, overlayFilters = []} = state || {},
			error = this.state.error,
			unit = false,
			{container, tooltipValue, showControls, radius, overlayRadius,
				viewState} = this.state,
			loading = !imageState,
			phenotype = getIn(imageState, ['phenotypes', layer]) || {},
			codes = (phenotype.int_to_category || []).slice(1),
			tooltipColor = this.getScale(phenotype)(tooltipValue),
			count = get(imageState, 'count'),
			name = titleProp || get(imageState, 'reference_name');

		return div({className: styles.content},
			div({className: styles.title},
				name ? span(name) : '',
				span({className: styles.spacer}),
				count ? span(`${count.toLocaleString()} cells`) : ''),
			span({className: styles.fps, ref: this.onFPSRef}),
			div({className: styles.graphWrapper, ref: this.onRef},
				controlsView({state: {radiusBase: 10, radius, overlayRadius},
					showControls, onControls, onRadius, onOverlayRadius,
					hasOverlay: !!overlay, onShadow}),
				get(state, 'showColorPicker') ? colorPicker({state, onState, layer}) :
					null,
				...(unit ? [scale(this.state.scale)] : []),
				...(tooltipValue != null ?
					[tooltipValueView(codes[tooltipValue], tooltipColor, onClose)]
					: []),
				getStatusView({loading, error, onReload, key: 'status'}),
				tiledScatterplot({...handlers, onViewState, onDeck, onTileData,
					onTooltip, radius, overlayRadius, viewState, hidden, referenceFilters, image,
					imageState, overlay, overlayFilters, hideOverlay, layer, container,
					key: 'drawing'})));
	}
});
