'use strict';

exports.rules = {
	'no-assignments': function (context) {
		return {
			AssignmentExpression: (node) => {
				if (node.left.type === 'Identifier') {
					context.report(node, 'Avoid assignment, or complain to bc.');
				}
			}
		};
	},
	'no-property-assignments': function (context) {
		return {
			AssignmentExpression: (node) => {
				if (node.left.type === 'MemberExpression' &&
					 node.left.object.type === 'Identifier' &&
					  node.left.object.name !== 'exports' &&
					  node.left.object.name !== 'module') {
					context.report(node, 'Avoid assignment, or complain to bc.');
				}
			}
		};
	}
};
