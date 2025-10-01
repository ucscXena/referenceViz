import {Button, Dialog, DialogActions, DialogContent,
	DialogTitle} from '@material-ui/core';
import {el, span} from './react-hyper';
import {tableLayout} from './muiTable';
import {colorScale, categoryMore as colors} from './colorScales';
import {assoc, Let, merge, range} from './underscore_ext.js';
import cmpCodes from './cmpCodes';
import setScale from './setScale';

var button = el(Button);
var dialog = el(Dialog);
var dialogActions = el(DialogActions);
var dialogContent = el(DialogContent);
var dialogTitle = el(DialogTitle);

import styles from './colorPicker.module.css';

var onCellClick = ({onState}) => ev => {
	if (ev.target.tagName === 'SPAN') {
		var cat = ev.target.parentElement.parentElement.dataset.code;
		var color = ev.target.parentElement.cellIndex - 1;
		onState(state => merge(state,
			{customColor: assoc(state.customColor, cat, colors[color])}));
	}
};

var colorTable = ({onState, codes, scale}) =>
  tableLayout({className: styles.table, onClick: onCellClick({onState})},
              ['Category', ...colors.map(c => span({style: {backgroundColor: c}}))],
              range(codes.length).sort(cmpCodes(codes)).reverse().map(
                i => [{'data-code': i}, codes[i], ...colors.map(c =>
                  span({style: {backgroundColor:
                    scale(i) === c ? c : 'rgba(0, 0, 0, 0)'}}, ''))]));

var onClose = (state, onState) => () =>
	onState(state => merge(state, {showColorPicker: false}));

export default ({onState, state, layer}) =>
  !state || !state.imageState ? null :
  Let(({imageState, customColor, hidden} = state,
		codes = imageState.phenotypes[layer].int_to_category.slice(1),
	  	scale = setScale(['ordinal', codes.length, customColor], hidden)) =>
    dialog({open: true, fullWidth: true, maxWidth: 'md', className: styles.dialog},
           dialogTitle('Edit colors'),
      dialogContent(colorTable({state, onState, codes, scale: colorScale(scale)})),
      dialogActions(button({onClick: onClose(state, onState)}, 'Close'))));
