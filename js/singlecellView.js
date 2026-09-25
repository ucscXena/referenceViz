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

var hoverTooltipView = ({label, color}, onClose, frozen, hasScale) =>
	div({className: styles.tooltip, style: {top: hasScale ? '28px' : '4px'}},
		color ? div({className: legendStyles.colorBox, style: {backgroundColor: color}}) : null,
		Array.isArray(label)
			? div({className: styles.tooltipLines}, ...label.map(l => div(l)))
			: label,
		frozen ? icon({onClick: onClose, className: styles.tooltipClose}, 'close') : null
	);

var detailPanelView = (rows, onClose) =>
	div({className: styles.detailPanel},
		div({className: styles.detailPanelHeader},
			span('Cell metadata'),
			icon({onClick: onClose, className: styles.detailPanelClose}, 'close')),
		div({className: styles.detailPanelBody},
			...rows.flatMap(({key, value}) => [
				span({className: styles.detailKey, title: key}, key),
				span({className: styles.detailValue, title: value}, value)
			])));

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
		presignOverlay(path).flatMap(({url, original_filename: originalFilename,
				cell_count: overlayCount}) =>
			fetchOverlay(url).map(ipc => ({ipc, originalFilename, overlayCount}))) :
		fetchOverlay(path).map(ipc => ({ipc, originalFilename: 'Mapped cells', overlayCount: undefined}));

function forceRedraw(deck) {
	if (deck) {
		deck.deck.setProps({}); // triggers re-render
		deck.deck.redraw(true); // explicit redraw
	}
}

export default el(class SinglecellView extends PureComponent {
	state = {
		hoverTooltip: null,
		tooltipFrozen: false,
		detailPanel: null,
		selectedPoint: null,
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
				({ipc, originalFilename, overlayCount}) => {
					var table = tableFromIPC(ipc);
					var names = pluck(table.schema.fields, 'name');
					var dicts = table.batches[0].data.children.map(f =>
						f.dictionary && f.dictionary.toArray());
					var ordered = table.schema.fields.map(f => !!f.type?.isOrdered);
					var data = pluck(table.batches[0].data.children, 'values');
					var overlay = assoc(assoc(object(names, data), '_dicts',
						object(names, dicts)), '_ordered', object(names, ordered));
					var overlayVars = without(names, 'x', 'y');
					var overlayFilters = overlayVars.length ?
						[{var: overlayVars[0], filtered: []}] : [];
					this.setState({overlayRadius: defaultOverlayRadius(overlay.x.length)});
					this.props.onState(state => merge(state, {overlay, overlayFilters,
						...(originalFilename ? {overlayTitle: originalFilename} : {}),
						...(overlayCount != null ? {overlayCount} : {})}));
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
	_refTooltip = i => {
		if (i == null) { return null; }
		var {imageState, layer} = this.props.state || {};
		var phenotype = getIn(imageState, ['phenotypes', layer]) || {};
		var codes = (phenotype.int_to_category || []).slice(1);
		return {label: codes[i], color: this.getScale(phenotype)(i), source: 'ref'};
	};
	// onTooltip and onOverlayTooltip both fire on every hover event (from different
	// branches of tiledScatterplot.onHover), each passing undefined for the other's
	// source. Track source so each handler only clears its own tooltip, not the other's.
	onTooltip = i => {
		if (this.state.tooltipFrozen || this.state.detailPanel) { return; }
		if (i == null) {
			if (this.state.hoverTooltip?.source === 'ref') {
				this.setState({hoverTooltip: null});
			}
		} else {
			this.setState({hoverTooltip: this._refTooltip(i)});
		}
	};
	onOverlayTooltip = entries => {
		if (this.state.tooltipFrozen || this.state.detailPanel) { return; }
		if (!entries) {
			if (this.state.hoverTooltip?.source === 'overlay') {
				this.setState({hoverTooltip: null});
			}
		} else {
			this.setState({hoverTooltip: {
				label: entries.map(({varName, value}) => `${varName}: ${value}`),
				color: entries[0]?.color,
				source: 'overlay'
			}});
		}
	};
	onTooltipClick = i => {
		if (i !== undefined) {
			this.setState({tooltipFrozen: true, hoverTooltip: this._refTooltip(i), detailPanel: null});
		} else {
			this.setState({tooltipFrozen: false, hoverTooltip: null, detailPanel: null, selectedPoint: null});
		}
	};
	onClose = () => {
		this.setState({hoverTooltip: null, tooltipFrozen: false, selectedPoint: null});
	};
	onDetailPanel = rows => {
		this.setState({detailPanel: rows, hoverTooltip: null, tooltipFrozen: false});
	};
	onCloseDetail = () => {
		this.setState({detailPanel: null, selectedPoint: null});
	};
	onSelectPoint = point => {
		this.setState({selectedPoint: point || null});
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
				hideOverlay, overlayFilters = [], overlayTitle, overlayCount} = state || {},
			error = this.state.error,
			unit = false,
			{container, hoverTooltip, tooltipFrozen, detailPanel, selectedPoint,
				showControls, radius, overlayRadius, viewState} = this.state,
			loading = !imageState,
			count = get(imageState, 'count'),
			name = titleProp || get(imageState, 'reference_name');

		return div({className: styles.content},
			div({className: styles.title},
				name ? span(count ? `${name} (${count.toLocaleString()} cells)` : name) : '',
				overlay && overlayTitle ?
					span(` / ${overlayTitle} (${(overlayCount != null ? overlayCount : overlay.x.length).toLocaleString()} cells)`) :
					''),
			span({className: styles.fps, ref: this.onFPSRef}),
			div({className: styles.graphWrapper, ref: this.onRef},
				controlsView({state: {radiusBase: 10, radius, overlayRadius},
					showControls, onControls, onRadius, onOverlayRadius,
					hasOverlay: !!overlay, onShadow}),
				get(state, 'showColorPicker') ? colorPicker({state, onState, layer}) :
					null,
				...(unit ? [scale(this.state.scale)] : []),
				...(hoverTooltip ?
					[hoverTooltipView(hoverTooltip, onClose, tooltipFrozen, unit)]
					: []),
				...(detailPanel ?
					[detailPanelView(detailPanel, this.onCloseDetail)]
					: []),
				getStatusView({loading, error, onReload, key: 'status'}),
				tiledScatterplot({...handlers, onViewState, onDeck, onTileData,
					onTooltip, onOverlayTooltip: this.onOverlayTooltip,
					onTooltipClick: this.onTooltipClick, onDetailPanel: this.onDetailPanel,
					onSelectPoint: this.onSelectPoint, selectedPoint,
					radius, overlayRadius, viewState, hidden, referenceFilters, image,
					imageState, overlay, overlayFilters, hideOverlay, layer, container,
					key: 'drawing'})));
	}
});
