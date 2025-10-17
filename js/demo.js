// API should be path to img, and view options.
import parseURL from './parseURL';
import ReactDOM from 'react-dom';
import singlecellView from './singlecellView';
import singlecellLegend from './singlecellLegend';
import {div, el} from './react-hyper';
import styles from './demo.module.css';
import {MuiThemeProvider, CssBaseline} from '@material-ui/core';
import {xenaTheme as theme} from './theme';

var muiThemeProvider = el(MuiThemeProvider);
var cssBaseline = el(CssBaseline);

var {path, params} = parseURL(window.location.href);
var segments = path.slice(1).replace(/\/$/, '').split(/\//);

if (segments[0] === 'pyramid') {
	var main = document.getElementById('main');
	main.style.position = 'relative';
	var state = {/*showColorPicker: true,*/ layer: 0}, onState;
	var render = () => {
		ReactDOM.render(
			muiThemeProvider({theme},
				cssBaseline(),
				div({className: styles.singlecell},
					singlecellView({
						image: params.image,
						state,
						onState}),
					singlecellLegend(state, onState))),
			main);
	};
	onState = fn => {
		state = fn(state);
		render();
	};
	render();
} else {
	document.body.innerHTML = `${segments[0]} not found`;
}
