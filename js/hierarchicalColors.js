import {RGBToHex} from './color_helper';

// Convert HSL (h: 0-360, s: 0-100, l: 0-100) to a hex color string.
var hslToHex = (h, s, l) => {
	var s1 = s / 100, l1 = l / 100,
		c = (1 - Math.abs(2 * l1 - 1)) * s1,
		x = c * (1 - Math.abs(h / 60 % 2 - 1)),
		m = l1 - c / 2,
		[r, g, b] = h < 60  ? [c, x, 0] :
		            h < 120 ? [x, c, 0] :
		            h < 180 ? [0, c, x] :
		            h < 240 ? [0, x, c] :
		            h < 300 ? [x, 0, c] :
		                      [c, 0, x];
	return RGBToHex(
		Math.round((r + m) * 255),
		Math.round((g + m) * 255),
		Math.round((b + m) * 255));
};

// Hue offsets for the wedge: start at center, then expand outward in both
// directions so adjacent sublabels stay visually related but distinct.
var wedgeHueShifts = [0, 15, -15, 20, -20, 25, -25, 10, -10, 30, -30, 35, -35];

// Dark → medium → light luminance strips, cycled for each hue position.
var wedgeLightnesses = [38, 54, 68];

var wedgeSaturation = 70;

// Generate 'count' colors in a wedge pattern around baseHue.
// Each group of three consecutive colors uses the same hue shift and cycles
// through dark / medium / light luminance, then moves to the next hue shift.
var wedgeColors = (baseHue, count) =>
	Array.from({length: count}, (_, i) =>
		hslToHex(
			(baseHue + wedgeHueShifts[Math.floor(i / 3) % wedgeHueShifts.length] + 360) % 360,
			wedgeSaturation,
			wedgeLightnesses[i % 3]));

// Generate a { codeIndex: hexColor } mapping from a taxonomy and a label list.
//
// groups: { groupName: [label, ...], ... }   (any hierarchical taxonomy)
// codes:  string[] indexed by integer code    (from imageState phenotypes)
//
// Top-level groups each receive a distinct hue evenly spaced around the color
// wheel. Within each group the wedge strategy applies: small hue rotations
// (± 15°) combined with alternating dark / medium / light luminance strips.
// Codes absent from the taxonomy fall back to a neutral gray.
export default (groups, codes) => {
	var groupNames = Object.keys(groups),
		numGroups = groupNames.length,
		lookup = {},
		customColor = {};

	codes.forEach((label, i) => { lookup[label] = i; });

	groupNames.forEach((name, groupIdx) => {
		var baseHue = Math.round(groupIdx * 360 / numGroups),
			groupCodes = groups[name]
				.map(l => lookup[l])
				.filter(c => c != null && !(c in customColor)),
			colors = wedgeColors(baseHue, groupCodes.length);
		groupCodes.forEach((code, i) => { customColor[code] = colors[i]; });
	});

	// Codes not covered by the taxonomy get a neutral gray
	codes.forEach((_, code) => {
		if (!(code in customColor)) {
			customColor[code] = '#888888';
		}
	});

	return customColor;
};
