import PureComponent from './PureComponent';
import {el} from './react-hyper';
import DeckGL from '@deck.gl/react';
import {OrthographicView} from '@deck.gl/core';
import {ScatterplotLayer} from '@deck.gl/layers';
import {DataFilterExtension} from '@deck.gl/extensions';
var scatterplotLayer = ({id, ...props}) => new ScatterplotLayer({id, ...props});
import {COORDINATE_SYSTEM} from '@deck.gl/core';
import {TileLayer} from '@deck.gl/geo-layers';
import {debounce} from './rx';
import {get, getIn, Let, memoize1} from './underscore_ext.js';
import '@luma.gl/debug';
import upng from 'upng-js';
import {phenotypeScale, categoryMore, categoryMoreRgb, sampleViridis} from './colorScales';
import {RGBToHex} from './color_helper.js';

var deckGL = el(DeckGL);

var get8Value = ({data, width}) => (x, y) => data[x + y * width];
var get16Value = ({data, width}) =>
	(x, y) => Let((offset = (x + y * width) << 1) =>
					(data[offset] << 8) + data[offset + 1]);

var getValue = png => png.depth > 8 ? get16Value(png) : get8Value(png);

function getCoords(colorPng, filterPngs) {
	const {width, height} = colorPng,
		getColorValue = getValue(colorPng),
		getFilterValues = filterPngs.map(getValue),
		pts = [];

	for (let j = 0; j < height; j++)  {
		for (let i = 0; i < width; ++i) {
			var c = getColorValue(i, j);
			if (!c) { continue; }
			// 0 is "no data". Decrement to get ordinal scale.
			var fs = getFilterValues.map(fn => fn(i, j));
			if (fs.every(f => f)) {
				pts.push([i, j, c - 1, ...fs.map(f => f - 1)]);
			}
		}
	}
	return pts;
}

var filterFn = referenceFilters =>
	Let((hiddenSets = referenceFilters.map(f => new Set(f.filtered))) =>
		d => [0, 1, 2].map(i =>
			i < hiddenSets.length && hiddenSets[i].has(d[3 + i]) ? 0 : 1));

var highlightFn = hideColors =>
	!hideColors || !hideColors.length ? () => 1 :
		Let((hidden = new Set(hideColors)) =>
			([, , c]) => hidden.has(c) ? 0 : 1);

const scatterplotTile = ({data, id, highlight, modelMatrix, colorfn, referenceFilters, radius}) =>
	scatterplotLayer({
		id: `scatter-plot-${id}`,
		data,
		modelMatrix: modelMatrix,
		pickable: true,
		antialiasing: false,
		getPosition: ([x, y]) =>  [x, y], // XXX switch to passing buffers?
		radiusUnits: 'pixels',
		getRadius: highlight.length ?
			Let((fn = highlightFn(highlight)) => d => fn(d) ? radius : radius + 3) :
			radius,
		radiusMinPixels: 0.5,
		getFillColor: ([, , c]) =>  colorfn.rgb(c), // XXX switch to passing buffers?
		getFilterValue: filterFn(referenceFilters), // XXX switch to passing buffers?
		filterRange: [[1, 1], [1, 1], [1, 1]],
		filterEnabled: referenceFilters.length > 0,
		updateTriggers: {getFilterValue: [referenceFilters], getFillColor: [colorfn],
			getRadius: [highlight, radius]},
		extensions: [new DataFilterExtension({filterSize: 3})]
	});

// scale and offset
var getM = (s, [x, y, z = 0]) => [
	s, 0, 0, 0,
	0, s, 0, 0,
	0, 0, s, 0,
	x, y, z, 1
];

var filterUrl = ({path, index: {x, y, z}, filterLayer, fileformat}) =>
	`${path}/${filterLayer}-${z}-${y}-${x}.${fileformat}`;

var imgPromise = (url, signal) =>
	fetch(url, {signal}).then(r => r.blob()).then(b => b.arrayBuffer())
		.then(b => upng.decode(b));

var tileLayer = ({fileformat, index, levels, name, referenceFilters, opacity, path,
	highlight, colorfn, size, tileSize, visible, radius,
	onTileData}) =>
	new TileLayer({
		id: `tile-layer-${index}-${referenceFilters.map(f => f.layer).join('-') || name}`,
		data: `${path}/${name}-{z}-{y}-{x}.${fileformat}`,
		loadOptions: {
			fetch: {
				credentials: 'include',
				headers: {
					'X-Redirect-To': location.origin
				}
			}
		},
		onViewportLoad: tiles => {
			onTileData(tiles
				.filter(t => t.content)
				.map(t => ({points: t.content, index: t.index})));
		},
		getTileData: ({url, signal, index}) => {
			var colorPromise = imgPromise(url, signal),
				filterPromises = referenceFilters.map(f =>
					`p${f.layer}` === name ? colorPromise :
						imgPromise(filterUrl({filterLayer: `p${f.layer}`, index, fileformat, path}),
							signal));
			return Promise.all([colorPromise, ...filterPromises])
					.then(([colorImg, ...filterImgs]) => {
				if (signal.aborted) {return null;}
				// Combine color and filter values: [x, y, colorValue, f1, f2, ...]
				return getCoords(colorImg, filterImgs);
			});
		},
		minZoom: 0,
		maxZoom: levels - 1,
		tileSize,
		// extent appears to be in the dimensions of the lowest-resolution image.
		extent: [0, 0, size[0], size[1]],
		opacity: 1.0,
		zoomOffset: 0,
		refinementStrategy: 'no-overlap',
		visible,
		// Have to include 'opacity' in props to force an update, because the
		// update algorithm doesn't see sublayer props.
		limits: opacity, // XXX does this do anything?
		renderSubLayers: props => {
			var data = props.data;
			var {x, y, z} = props.tile.index;
			var modelMatrix = getM(1 / (1 << z),
				[x * tileSize >> z, y * tileSize >> z]);
			return scatterplotTile(
				{data, id: `${z}-${y}-${x}`, modelMatrix, colorfn, highlight, referenceFilters, radius});
		},
		updateTriggers: {
			renderSubLayers: [colorfn, referenceFilters, radius, highlight]
		}
	});


var initialZoom = props => {
	var {width, height} = props.container.getBoundingClientRect(),
		{imageState: {size: [iwidth, iheight]}} = props;

	return Math.log2(Math.min(0.8 * width / iwidth, 0.8 * height / iheight));
};

var currentScale = (levels, zoom, scale) => Math.pow(2, levels - zoom - 1) / scale;

var overlayLayer = ({data, modelMatrix, overlayRadius, visible, overlayFilters = [], overlayColorVar}) =>
	new ScatterplotLayer({
		id: 'scatterplot-overlay',
		data: {...data, length: data.x.length},
		visible,
		modelMatrix,
		pickable: true,
		antialiasing: false,
		// XXX switch to buffers
		getPosition: (_, {index, data}) =>  [data.x[index], data.y[index]],
		radiusUnits: 'pixels',
		getRadius: overlayRadius,
		radiusMinPixels: 0.5,
		stroked: true,
		getLineColor: [0, 0, 0, 200],
		lineWidthUnits: 'pixels',
		lineWidthMinPixels: 1,
		getFillColor: overlayColorVar
			? Let((isOrdered = data._ordered?.[overlayColorVar],
				count = data._dicts?.[overlayColorVar]?.length ?? 1) =>
				(_, {index, data: d}) => {
					var code = d[overlayColorVar]?.[index];
					if (code == null || code < 0) { return [180, 180, 180]; }
					return isOrdered
						? sampleViridis(count <= 1 ? 0.5 : code / (count - 1))
						: categoryMoreRgb[code % categoryMoreRgb.length];
				})
			: [0, 0, 0],
		updateTriggers: {
			getRadius: [overlayRadius],
			getFilterValue: [overlayFilters],
			getFillColor: [overlayColorVar],
		},
		filterRange: [[1, 1], [1, 1], [1, 1]],
		filterEnabled: overlayFilters.length > 0,
		getFilterValue: Let((hiddenSets = overlayFilters.map(f => new Set([-1, ...f.filtered]))) =>
			(_, {index, data}) => [0, 1, 2].map(i =>
				i < hiddenSets.length && hiddenSets[i].has(data[overlayFilters[i].var][index]) ? 0 : 1)),
		extensions: [new DataFilterExtension({filterSize: 3})],
	});

// Tile PNG cache shared across component instances: URL → Promise<upng.Image>
// LRU eviction via Map insertion order: hit → delete+reinsert (moves to end);
// miss → evict first entry when full, then insert.
const TILE_CACHE_MAX = 500;
const tileCache = new Map();

class TiledScatterplot extends PureComponent {
	static displayName = 'TiledScatterplot';
	getScale = memoize1(phenotypeScale);
	_initialViewState = null;
	_views = new OrthographicView({far: -1, near: 1});
	_pendingClickId = 0;

	onHover = debounce(60, ev => {
		if (ev.index >= 0 && ev.tile) {
			let [, , i] = ev.tile.layers[0].props.data[ev.index];
			this.props.onTooltip(i);
			this.props.onOverlayTooltip(undefined);
		} else if (ev.index >= 0 && ev.layer?.id === 'scatterplot-overlay') {
			const {overlay, overlayFilters} = this.props;
			const activeFilters = overlay ? (overlayFilters || []) : [];
			if (activeFilters.length > 0) {
				const entries = activeFilters.map((f, fi) => {
					const code = overlay[f.var]?.[ev.index];
					const dict = overlay._dicts?.[f.var];
					const value = dict
						? (code < 0 ? '—' : (dict[code] ?? String(code)))
						: String(code ?? '');
					if (fi === 0 && code != null && code >= 0) {
						const isOrdered = overlay._ordered?.[f.var];
						const count = dict?.length ?? 1;
						const color = isOrdered
							? RGBToHex(...sampleViridis(count <= 1 ? 0.5 : code / (count - 1)))
							: categoryMore[code % categoryMore.length];
						return {varName: f.var, value, color};
					}
					return {varName: f.var, value};
				});
				this.props.onOverlayTooltip(entries);
			} else {
				this.props.onOverlayTooltip(undefined);
			}
			this.props.onTooltip(undefined);
		} else {
			this.props.onTooltip(undefined);
			this.props.onOverlayTooltip(undefined);
		}
	});
	_fetchTile = (phenotypeIndex, tileIndex) => {
		var {image, imageState: {fileformat = 'png'}} = this.props;
		var {x, y, z} = tileIndex;
		var url = `${image}/p${phenotypeIndex}-${z}-${y}-${x}.${fileformat}`;
		if (tileCache.has(url)) {
			var hit = tileCache.get(url);
			tileCache.delete(url);
			tileCache.set(url, hit); // move to end (most recently used)
			return hit;
		}
		if (tileCache.size >= TILE_CACHE_MAX) {
			tileCache.delete(tileCache.keys().next().value); // evict LRU
		}
		var promise = fetch(url, {credentials: 'include', headers: {'X-Redirect-To': location.origin}})
			.then(r => r.blob())
			.then(b => b.arrayBuffer())
			.then(b => upng.decode(b));
		tileCache.set(url, promise);
		return promise;
	};
	_fetchAllPhenotypes = async (px, py, tileIndex) => {
		var {imageState: {phenotypes = []}} = this.props;
		return Promise.all(phenotypes.map(async (phenotype, i) => {
			var img = await this._fetchTile(i, tileIndex);
			var fn = img.depth > 8 ? get16Value(img) : get8Value(img);
			var pixelValue = fn(px, py);
			var cats = phenotype.int_to_category || [];
			var value = pixelValue === 0 ? '—' : (cats[pixelValue] ?? String(pixelValue));
			return {key: phenotype.name, value};
		}));
	};
	onTooltipClick = async ev => {
		if (ev.index >= 0 && ev.tile) {
			var clickId = ++this._pendingClickId;
			let [px, py, colorCode] = ev.tile.layers[0].props.data[ev.index];
			this.props.onTooltipClick(colorCode); // freeze hover tooltip while loading
			var {imageState} = this.props;
			var {image_scalef: scale = 1, offset = [0, 0]} = imageState;
			var adj = 1 << (imageState.levels - 1);
			var s = scale / adj;
			var [wx, wy] = ev.coordinate || [0, 0];
			this.props.onSelectPoint({x: (wx - offset[0] / adj) / s, y: (wy - offset[1] / adj) / s});
			var rows = await this._fetchAllPhenotypes(px, py, ev.tile.index);
			if (clickId !== this._pendingClickId) { return; } // superseded by newer click
			this.props.onDetailPanel(rows);
		} else if (ev.index >= 0 && ev.layer?.id === 'scatterplot-overlay') {
			++this._pendingClickId;
			var {overlay} = this.props;
			if (overlay) {
				var names = Object.keys(overlay).filter(k => k !== 'x' && k !== 'y' && k !== '_dicts');
				var overlayRows = names.map(varName => {
					var code = overlay[varName]?.[ev.index];
					var dict = overlay._dicts?.[varName];
					var value = dict
						? (code < 0 ? '—' : (dict[code] ?? String(code)))
						: String(code ?? '');
					return {key: varName, value};
				});
				this.props.onDetailPanel(overlayRows);
				this.props.onSelectPoint({x: overlay.x[ev.index], y: overlay.y[ev.index]});
			}
		} else {
			++this._pendingClickId;
			this.props.onTooltipClick(undefined);
			this.props.onSelectPoint(null);
		}
	};
	onViewState = debounce(400, this.props.onViewState);
	componentDidMount() {
		var zoom = get(this.props.viewState, 'zoom', initialZoom(this.props)),
			{image: {image_scalef: scale}, imageState: {levels}} = this.props;
		this.props.onViewState(null, currentScale(levels, zoom, scale));
	}
	render() {
		var {props} = this,
			{layer, onTileData} = props,
			// XXX color0? Probably should be cut
			{image, imageState, overlay, overlayFilters = [],
				hideOverlay, radius, overlayRadius, hidden = [], referenceFilters = [],
				selectedPoint} = props,
			phenotype = getIn(imageState, ['phenotypes', layer]) || {},
			colorfn = this.getScale(phenotype),
			{image_scalef: scale = 1, offset = [0, 0]} = imageState,
			adj = (1 << imageState.levels - 1),
			modelMatrix = getM(scale / adj, offset.map(c => c / adj));

		var {levels, size: [iwidth, iheight], fileformat = 'png'} = imageState;

		if (!this._initialViewState) {
			var zoom = initialZoom(props);
			this._initialViewState = {
				zoom,
				minZoom: zoom,
				maxZoom: levels,
				target: [iwidth / 2, iheight / 2]
			};
		}

		return deckGL({
			ref: this.props.onDeck,
			onViewStateChange: ({viewState}) => {
				this.onViewState(viewState,
					currentScale(levels, viewState.zoom, scale));
			},
			layers: [ // XXX expand to multiple channels?
				tileLayer({
					name: `p${layer}`, path: image,
					referenceFilters,
					fileformat,
					highlight: hidden,
					index: 'phenotype', // XXX review this
					levels: imageState.levels,
					size: imageState.size,
					tileSize: imageState.tileSize,
					visible: true,
					colorfn,
					radius,
					onTileData
				}),
				...(overlay ? [overlayLayer({data: overlay, visible: !hideOverlay,
					overlayRadius, modelMatrix, overlayFilters,
					overlayColorVar: overlayFilters[0]?.var})] : []),
			...(selectedPoint ? [
				new ScatterplotLayer({
					id: 'selected-ring-outer',
					data: [selectedPoint],
					getPosition: d => [d.x, d.y],
					modelMatrix,
					stroked: true,
					filled: false,
					getRadius: 10,
					radiusUnits: 'pixels',
					getLineColor: [0, 0, 0, 210],
					lineWidthUnits: 'pixels',
					lineWidthMinPixels: 2,
				}),
				new ScatterplotLayer({
					id: 'selected-ring-inner',
					data: [selectedPoint],
					getPosition: d => [d.x, d.y],
					modelMatrix,
					stroked: true,
					filled: false,
					getRadius: 7,
					radiusUnits: 'pixels',
					getLineColor: [255, 255, 255, 255],
					lineWidthUnits: 'pixels',
					lineWidthMinPixels: 2,
				}),
			] : [])
			],
			views: this._views,
			controller: true,
			coordinateSystem: COORDINATE_SYSTEM.CARTESIAN,
			getCursor: () => 'inherit',
			initialViewState: this._initialViewState,
			onHover: this.onHover,
			onClick: this.onTooltipClick,
			style: {backgroundColor: '#FFFFFF'}
		});
	}
}
var comp = el(TiledScatterplot);

export default el(props =>
		(!props.container || !props.imageState) ? null :
		comp(props));

