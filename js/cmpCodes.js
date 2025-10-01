import {Let} from './underscore_ext';

var cmpStr = (i, j) => i < j ? 1 : j < i ? -1 : 0;

export default codes => (i, j) =>
	Let((ci = codes[i], cj = codes[j]) =>
		isNaN(ci) && isNaN(cj) ? cmpStr(ci, cj) :
		isNaN(ci) ? 1 :
		isNaN(cj) ? -1 :
		+cj - +ci);


