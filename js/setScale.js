import {constant, merge, object, updateIn} from './underscore_ext.js';

export var ORDINAL = {
	VARIANT: 0,
	COLORS: 1,
	CUSTOM: 2
};

var gray = '#F0F0F0';
export default (scale, hidden) =>
	hidden ? updateIn(scale, [ORDINAL.CUSTOM], custom =>
			merge(custom, object(hidden, hidden.map(constant(gray)))))
		: scale;


