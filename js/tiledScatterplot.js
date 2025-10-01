import PureComponent from './PureComponent';
import {el} from './react-hyper';
import DeckGL from '@deck.gl/react';
import {OrthographicView} from '@deck.gl/core';
//import {scatterplotLayer} from '../ScatterplotLayer';
import {ScatterplotLayer} from '@deck.gl/layers';
import {DataFilterExtension} from '@deck.gl/extensions';
var scatterplotLayer = ({id, ...props}) => new ScatterplotLayer({id, ...props});
import {COORDINATE_SYSTEM} from '@deck.gl/core';
import {TileLayer} from '@deck.gl/geo-layers';
import {debounce} from './rx';
import {get, Let} from './underscore_ext.js';
import '@luma.gl/debug';
import {categoryMoreRgb} from './colorScales';
import upng from 'upng-js';

var deckGL = el(DeckGL);

var get8Coords = (width, height, data) => {
	const pts = [];
	for (let j = 0; j < height; j++)  {
		for (let i = 0; i < width; ++i) {
			var c = data[i + j * width];
			if (c) {
				// 0 is "no data". Decrement to get ordinal scale.
				pts.push([i, j, c - 1]);
			}
		}
	}
	return pts;
};

// Neither typed arrays nor DataView offer a way to read
// a big endian array in a generic way. So, swapping the bytes here.
var get16Coords = (width, height, data) => {
	const pts = [];
	for (let j = 0; j < height; j++)  {
		for (let i = 0; i < width; ++i) {
			var k = (i + j * width) << 1;
			var c = (data[k] << 8) + data[k + 1];
			if (c) {
				// 0 is "no data". Decrement to get ordinal scale.
				pts.push([i, j, c - 1]);
			}
		}
	}
	return pts;
};

var getCoords = img => {
	var {width, height, data, depth} = upng.decode(img);

	return (depth > 8 ? get16Coords : get8Coords)(width, height, data);
};

var colorfn = i => categoryMoreRgb[i % categoryMoreRgb.length];

var filterFn = hideColors =>
	!hideColors ? () => 1 :
		Let((hidden = new Set(hideColors)) =>
			([, , c]) => hidden.has(c) ? 0 : 1);

const scatterplotTile = ({data, id, modelMatrix, hideColors, radius}) =>
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
		getRadius: radius, //1, //radius,
		radiusMinPixels: 1,
		getFillColor: ([, , c]) =>  colorfn(c), // XXX switch to passing buffers?
		getFilterValue: filterFn(hideColors), // XXX switch to passing buffers?
		filterRange: [1, 1],
		updateTriggers: {getFilterValue: [hideColors]},
		extensions: [new DataFilterExtension({filterSize: 1})]
	});

// scale and offset
var getM = (s, [x, y, z = 0]) => [
	s, 0, 0, 0,
	0, s, 0, 0,
	0, 0, s, 0,
	x, y, z, 1
];

var tileLayer = ({fileformat, index, levels, name, opacity, path,
	size, tileSize, visible, hideColors, radius}) =>
	new TileLayer({
		id: `tile-layer-${index}`,
		data: `${path}/${name}-{z}-{y}-{x}.${fileformat}`,
		loadOptions: {
			fetch: {
				credentials: 'include',
				headers: {
					'X-Redirect-To': location.origin
				}
			}
		},
		getTileData: ({url, signal}) => {
			const data = fetch(url, {signal});

			if (signal.aborted) {
				return null;
			}
			return data.then(r => r.blob()).then(b => b.arrayBuffer())
				.then(getCoords);
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
				{data, id: `${z}-${y}-${x}`, modelMatrix, hideColors, radius});
		},
		updateTriggers: {
			renderSubLayers: [hideColors, radius]
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

	onTooltip = ev => {
		var {imageState, layer} = this.props;
		if (ev.index >= 0) {
			let codes = imageState.phenotypes[layer].int_to_category,
				[, , i] = ev.tile.layers[0].props.data[ev.index];
			this.props.onTooltip(codes[i + 1]);
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
			{layer} = props,
			// XXX color0? Probably should be cut
			{image, imageState, radius, hidden: hideColors = []} = props,
			{image_scalef: scale/*, offset*/} = image,
			// TileLayer operates on the scale of the smallest downsample.
			// Adjust the scale here for the number of downsamples, so the data
			// overlay lines up.
			adj = (1 << imageState.levels - 1);

//		var modelMatrix = getM(scale / adj, offset.map(c => c / adj));

		radius = radius * scale / adj;

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
					fileformat,
					index: 'phenotype', // XXX review this
					levels: imageState.levels,
					size: imageState.size,
					tileSize: imageState.tileSize,
					visible: true,
					hideColors,
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

