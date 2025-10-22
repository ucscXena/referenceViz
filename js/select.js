import FormControl from '@material-ui/core/FormControl';
import InputLabel from '@material-ui/core/InputLabel';
import Select from '@material-ui/core/Select';
import {el} from './react-hyper';
var select = el(Select);
var formControl = el(FormControl);
var inputLabel = el(InputLabel);

export default ({label, id, ...props}, ...children) =>
	formControl(
		label && inputLabel({id}, label),
		select({labelId: label && id, variant: 'standard', ...props}, ...children));
