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
import {get, getIn, Let, memoize1, pluck} from './underscore_ext.js';
import '@luma.gl/debug';
import upng from 'upng-js';
import {colorScale} from './colorScales';
import setScale from './setScale';

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
			onTileData(pluck(tiles, 'content').filter(x => x));
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

var overlayLayer = ({data, modelMatrix, overlayRadius, visible, overlayFilters = []}) =>
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
		getFillColor: [0, 0, 0],
		updateTriggers: {
			getRadius: [overlayRadius],
			getFilterValue: [overlayFilters],
		},
		filterRange: [[1, 1], [1, 1], [1, 1]],
		filterEnabled: overlayFilters.length > 0,
		getFilterValue: Let((hiddenSets = overlayFilters.map(f => new Set([-1, ...f.filtered]))) =>
			(_, {index, data}) => [0, 1, 2].map(i =>
				i < hiddenSets.length && hiddenSets[i].has(data[overlayFilters[i].var][index]) ? 0 : 1)),
		extensions: [new DataFilterExtension({filterSize: 3})],
	});

class TiledScatterplot extends PureComponent {
	static displayName = 'TiledScatterplot';
	getScale = memoize1(codes =>
		colorScale(setScale(['ordinal', codes.length])));

	onTooltip = ev => {
		if (ev.index >= 0 && ev.tile) {
			let [, , i] = ev.tile.layers[0].props.data[ev.index];
			this.props.onTooltip(i);
		} else {
			this.props.onTooltip(undefined);
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
				hideOverlay, radius, overlayRadius, hidden = [], referenceFilters = []} = props,
			codes = getIn(imageState, ['phenotypes', layer, 'int_to_category'], [])
				.slice(1),
			colorfn = this.getScale(codes),
			{image_scalef: scale = 1, offset = [0, 0]} = imageState,
			adj = (1 << imageState.levels - 1),
			modelMatrix = getM(scale / adj, offset.map(c => c / adj));

		var views = new OrthographicView({far: -1, near: 1}),
			{levels, size: [iwidth, iheight],
				fileformat = 'png'} = imageState,
			zoom = initialZoom(props),
			viewState = {
				zoom,
				minZoom: zoom,
				maxZoom: levels,
				target: [iwidth / 2, iheight / 2]
			};

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
					overlayRadius, modelMatrix, overlayFilters})] : [])
			],
			views,
			controller: true,
			coordinateSystem: COORDINATE_SYSTEM.CARTESIAN,
			getCursor: () => 'inherit',
			initialViewState: props.viewState || viewState,
			onClick: this.onTooltip,
			style: {backgroundColor: '#FFFFFF'}
		});
	}
}
var comp = el(TiledScatterplot);

export default el(props =>
		(!props.container || !props.imageState) ? null :
		comp(props));

