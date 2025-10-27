import PureComponent from './PureComponent';
import Typography from '@material-ui/core/Typography';
import Icon from '@material-ui/core/Icon';
import {map, last, zip} from './underscore_ext.js';
import {div, el, label} from './react-hyper';

// Styles
import compStyles from "./legend.module.css";

import classNames from 'classnames';

var typography = el(Typography);
var icon = el(Icon);

var nodata = {
	text: "null: no data",
	color: "#808080",
};

class Legend extends PureComponent {
	static defaultProps = {max: 40};

	render() {
		var {labels, colors, titles, max, labelheader, footnotes, addBreakend = 0,
				codes, checked, addNullNotation = 0, inline, onClick} = this.props,
			style = classNames(onClick && compStyles.clickable,
				inline && compStyles.inline),
			ellipsis = labels.length > max,
			items = map(last(zip(labels, titles, codes), max),
				([l, t, code], i) =>
					div({key: i, 'data-code': code, title: t,
							className: compStyles.item},
						div({className: compStyles.colorBox,
								style: colors ? {backgroundColor: colors[i]} : {}},
							checked && checked[i] ? icon('check') : null),
						typography({component: 'label', className: compStyles.label,
							variant: 'caption'}, l))).reverse(),
			footnotesItems = footnotes ?
				footnotes.map((text, i) =>
						typography({key: i, component: 'div',
								className: compStyles.footnotes, variant: 'caption'},
							text)) : null,
			breakend =
				div({className: compStyles.item},
					div({className: compStyles.breakendBar}),
					typography({component: 'label', className: compStyles.label,
						variant: 'caption'}, 'breakend')),
			nullNotation =
				div({title: nodata.text},
					typography({component: 'label', className: compStyles.null,
							style: {backgroundColor: nodata.color}, variant: 'caption'},
						nodata.text));

		return (
			div({className: style},
				items ?
					div({className: compStyles.column, onClick},
						labelheader ? label({className: compStyles.header},
							labelheader) : null,
						items,
						addBreakend ? breakend : null,
						addNullNotation ? nullNotation : null) :
					null,
				ellipsis ? div('...') : null,
				footnotes ? footnotesItems : null));
	}
}

export default el(Legend);
