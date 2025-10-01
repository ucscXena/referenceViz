import {FormControl, InputLabel, Select} from '@material-ui/core';
import {el} from './react-hyper';
var select = el(Select);
var formControl = el(FormControl);
var inputLabel = el(InputLabel);

export default ({label, id, ...props}, ...children) =>
	formControl(
		label && inputLabel({id}, label),
		select({labelId: label && id, variant: 'standard', ...props}, ...children));
