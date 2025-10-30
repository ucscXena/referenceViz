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
import {colorScale} from './colorScales';
import setScale from './setScale';

var deckGL = el(DeckGL);

var get8Value = ({data, width}) => (x, y) => data[x + y * width];
var get16Value = ({data, width}) =>
	(x, y) => Let((offset = (x + y * width) << 1) =>
					(data[offset] << 8) + data[offset + 1]);

var getValue = png => png.depth > 8 ? get16Value(png) : get8Value(png);

function getCoords(png0, png1) {
	const {width, height} = png0,
		getColorValue = getValue(png0),
		getFilterValue = getValue(png1),
		pts = [];

	for (let j = 0; j < height; j++)  {
		for (let i = 0; i < width; ++i) {
			var c = getColorValue(i, j),
				f = getFilterValue(i, j);
			if (c) {
				// 0 is "no data". Decrement to get ordinal scale.
				pts.push([i, j, c - 1, f - 1]);
			}
		}
	}
	return pts;
}

var filterFn = hideColors =>
	!hideColors ? () => 1 :
		Let((hidden = new Set(hideColors)) =>
			([, , , c]) => hidden.has(c) ? 0 : 1);

var highlightFn = hideColors =>
	!hideColors || !hideColors.length ? () => 1 :
		Let((hidden = new Set(hideColors)) =>
			([, , c]) => hidden.has(c) ? 0 : 1);

const scatterplotTile = ({data, id, highlight, modelMatrix, colorfn, hideColors, radius}) =>
	scatterplotLayer({
		id: `scatter-plot-${id}`,
		data,
		modelMatrix: modelMatrix,
		getLineWidth: 5,
		pickable: true,
		antialiasing: false,
		getPosition: ([x, y]) =>  [x, y], // XXX switch to passing buffers?
		lineWidthMinPixels: 20,
		lineWidthMaxPixels: 800,
		radiusUnits: 'pixels',
		getRadius: highlight.length ?
			Let((fn = highlightFn(highlight)) => d => fn(d) ? radius : radius + 3) :
			radius,
		radiusMinPixels: 1,
		getFillColor: ([, , c]) =>  colorfn.rgb(c), // XXX switch to passing buffers?
		getFilterValue: filterFn(hideColors), // XXX switch to passing buffers?
		filterRange: [1, 1],
		updateTriggers: {getFilterValue: [hideColors], getFillColor: [colorfn],
			getRadius: [highlight, radius]},
		extensions: [new DataFilterExtension({filterSize: 1})]
	});

// scale and offset
var getM = (s, [x, y, z = 0]) => [
	s, 0, 0, 0,
	0, s, 0, 0,
	0, 0, s, 0,
	x, y, z, 1
];

var filterUrl = ({path,  index: {x, y, z}, filterLayer, fileformat}) =>
	`${path}/${filterLayer}-${z}-${y}-${x}.${fileformat}`;

var imgPromise = (url, signal) =>
	fetch(url, {signal}).then(r => r.blob()).then(b => b.arrayBuffer())
		.then(b => upng.decode(b));

var tileLayer = ({fileformat, index, levels, name, filterLayer, opacity, path,
	highlight, colorfn, size, tileSize, visible, filterColors, radius}) =>
	new TileLayer({
		id: `tile-layer-${index}-${filterLayer || name}`,
		data: `${path}/${name}-{z}-{y}-{x}.${fileformat}`,
		loadOptions: {
			fetch: {
				credentials: 'include',
				headers: {
					'X-Redirect-To': location.origin
				}
			}
		},
		getTileData: ({url, signal, index}) => {
			var colorPromise = imgPromise(url, signal),
				filterPromise = filterLayer && filterLayer !== name ?
					imgPromise(filterUrl({filterLayer, index, fileformat, path}),
						signal) :
					colorPromise;
			return Promise.all([colorPromise, filterPromise])
					.then(([colorImg, filterImg]) => {
				if (signal.aborted) {return null;}
				// Combine color and filter values: [x, y, colorValue, filterValue]
				return getCoords(colorImg, filterImg);
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
				{data, id: `${z}-${y}-${x}`, modelMatrix, colorfn, highlight, hideColors: filterColors, radius});
		},
		updateTriggers: {
			filterLayer,
			renderSubLayers: [colorfn, filterColors, radius]
		}
	});


var initialZoom = props => {
	var {width, height} = props.container.getBoundingClientRect(),
		{imageState: {size: [iwidth, iheight]}} = props;

	return Math.log2(Math.min(0.8 * width / iwidth, 0.8 * height / iheight));
};

var currentScale = (levels, zoom, scale) => Math.pow(2, levels - zoom - 1) / scale;

class TiledScatterplot extends PureComponent {
	static displayName = 'TiledScatterplot';
	getScale = memoize1((codes, hidden) =>
		colorScale(setScale(['ordinal', codes.length], hidden)));

	onTooltip = ev => {
		if (ev.index >= 0) {
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
			{layer, filterLayer} = props,
			// XXX color0? Probably should be cut
			{image, imageState, radius, hidden = [],
				filtered: filterColors = []} = props,
			codes = getIn(imageState, ['phenotypes', layer, 'int_to_category'], [])
				.slice(1),
			colorfn = this.getScale(codes, hidden),
			{image_scalef: scale/*, offset*/} = image;

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
					name: `p${layer}`, path: image.path,
					filterLayer: filterLayer >= 0 && `p${filterLayer}`,
					fileformat,
					highlight: hidden,
					index: 'phenotype', // XXX review this
					levels: imageState.levels,
					size: imageState.size,
					tileSize: imageState.tileSize,
					visible: true,
					colorfn,
					filterColors,
					radius
				}),
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

