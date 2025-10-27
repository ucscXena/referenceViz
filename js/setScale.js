import {constant, merge, object, updateIn} from './underscore_ext.js';

export var ORDINAL = {
	VARIANT: 0,
	COLORS: 1,
	CUSTOM: 2
};

var gray = '#000000';
export default (scale, hidden) =>
	hidden ? updateIn(scale, [ORDINAL.CUSTOM], custom =>
			merge(custom, object(hidden, hidden.map(constant(gray)))))
		: scale;


