"""Unit tests for row/column header annotation of interactive controls inside
native HTML tables.

Covers:
- explicit scope="row"/scope="col" association (also across thead/tbody)
- rowspan/colspan grid placement, including span="0"
- the ``headers`` attribute: same-table <th> references only, invalid/self
  references ignored, chained references, and no scope fallback
- hierarchical (multi-level) headers
- nested tables associated independently
- no guessing when no explicit association exists
- visibility / paint-order / explicit exclusion rules for header text
- elements outside tables keep an unchanged representation
"""

import itertools

from browser_use.dom.serializer.serializer import DOMTreeSerializer
from browser_use.dom.serializer.table_headers import annotate_table_headers
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, EnhancedSnapshotNode, NodeType, SimplifiedNode

_SNAPSHOT = EnhancedSnapshotNode(
	is_clickable=None,
	cursor_style=None,
	bounds=DOMRect(x=0, y=0, width=10, height=10),
	clientRects=None,
	scrollRects=None,
	computed_styles=None,
	paint_order=1,
	stacking_contexts=None,
)

_id_counter = itertools.count(1)


def _enhanced(node_type: NodeType, node_name: str, *, value: str = '', attrs=None, visible: bool = True):
	return EnhancedDOMTreeNode(
		node_id=next(_id_counter),
		backend_node_id=next(_id_counter),
		node_type=node_type,
		node_name=node_name,
		node_value=value,
		attributes=dict(attrs or {}),
		is_scrollable=None,
		is_visible=visible,
		absolute_position=None,
		target_id='test-target',
		frame_id=None,
		session_id=None,
		content_document=None,
		shadow_root_type=None,
		shadow_roots=None,
		parent_node=None,
		children_nodes=None,
		ax_node=None,
		snapshot_node=_SNAPSHOT if visible else None,
	)


def el(tag: str, *children: EnhancedDOMTreeNode, attrs=None) -> EnhancedDOMTreeNode:
	node = _enhanced(NodeType.ELEMENT_NODE, tag.upper(), attrs=attrs)
	if node.children_nodes is None:
		node.children_nodes = []
	for child in children:
		node.children_nodes.append(child)
		child.parent_node = node
	return node


def tx(value: str, *, visible: bool = True) -> EnhancedDOMTreeNode:
	return _enhanced(NodeType.TEXT_NODE, '#text', value=value, visible=visible)


def _to_simplified(
	root: EnhancedDOMTreeNode,
	*,
	interactive=(),
	skip=(),
	paint_ignored=(),
) -> SimplifiedNode | None:
	interactive_ids = {id(node) for node in interactive}
	skip_ids = {id(node) for node in skip}
	paint_ids = {id(node) for node in paint_ignored}
	selector_indices = itertools.count(100)

	def convert(enhanced: EnhancedDOMTreeNode) -> SimplifiedNode | None:
		if id(enhanced) in skip_ids:
			return None
		if enhanced.node_type == NodeType.TEXT_NODE:
			if not (enhanced.snapshot_node and enhanced.is_visible):
				return None
			simplified = SimplifiedNode(original_node=enhanced, children=[])
			simplified.ignored_by_paint_order = id(enhanced) in paint_ids
			return simplified

		children: list[SimplifiedNode] = []
		for child in enhanced.children:
			simplified_child = convert(child)
			if simplified_child is not None:
				children.append(simplified_child)

		simplified = SimplifiedNode(original_node=enhanced, children=children)
		if id(enhanced) in interactive_ids:
			simplified.is_interactive = True
			simplified.selector_index = next(selector_indices)
		simplified.ignored_by_paint_order = id(enhanced) in paint_ids
		return simplified

	return convert(root)


def _serialize(root: EnhancedDOMTreeNode, **kwargs) -> str:
	simplified = _to_simplified(root, **kwargs)
	annotate_table_headers(root, simplified)
	return DOMTreeSerializer.serialize_tree(simplified, ['id', 'name'])


def _line_for(output: str, element_id: str) -> str:
	lines = [line for line in output.splitlines() if f'id={element_id}' in line]
	assert len(lines) == 1, f'expected exactly one line for {element_id}, got: {lines}'
	return lines[0]


def _matrix_table() -> tuple[
	EnhancedDOMTreeNode,
	EnhancedDOMTreeNode,
	EnhancedDOMTreeNode,
	EnhancedDOMTreeNode,
	EnhancedDOMTreeNode,
]:
	"""
	+-------+-----+-----+
	|       | Jan | Feb |
	+-------+-----+-----+
	| Fruit |  a  |  b  |   (Fruit spans two rows)
	|       +-----+-----+
	|       |  c  |  d  |
	+-------+-----+-----+
	"""
	input_a = el('input', attrs={'id': 'a'})
	input_b = el('input', attrs={'id': 'b'})
	input_c = el('input', attrs={'id': 'c'})
	input_d = el('input', attrs={'id': 'd'})
	table = el(
		'table',
		el(
			'thead',
			el(
				'tr',
				el('th'),
				el('th', tx('Jan'), attrs={'scope': 'col'}),
				el('th', tx('Feb'), attrs={'scope': 'col'}),
			),
		),
		el(
			'tbody',
			el(
				'tr',
				el('th', tx('Fruit'), attrs={'scope': 'row', 'rowspan': '2'}),
				el('td', input_a),
				el('td', input_b),
			),
			el(
				'tr',
				el('td', input_c),
				el('td', input_d),
			),
		),
	)
	return table, input_a, input_b, input_c, input_d  # type: ignore[return-value]


class TestTableHeaderAnnotation:
	def test_scope_row_and_col_headers_basic(self):
		table, input_a, _, _, _ = _matrix_table()
		output = _serialize(table, interactive=[input_a])

		line = _line_for(output, 'a')
		assert 'table_headers=Jan | Fruit' in line
		# Original attributes and the element index behavior are preserved.
		assert '[100]<input' in line
		assert 'id=a ' in line

	def test_rowspan_associates_header_with_all_spanned_rows(self):
		table, input_a, _, input_c, _ = _matrix_table()
		output = _serialize(table, interactive=[input_a, input_c])

		assert 'table_headers=Jan | Fruit' in _line_for(output, 'a')
		assert 'table_headers=Jan | Fruit' in _line_for(output, 'c')

	def test_each_column_gets_its_own_col_header(self):
		table, _, input_b, _, input_d = _matrix_table()
		output = _serialize(table, interactive=[input_b, input_d])

		assert 'table_headers=Feb | Fruit' in _line_for(output, 'b')
		assert 'table_headers=Feb | Fruit' in _line_for(output, 'd')

	def test_colspan_header_covers_each_spanned_column(self):
		input_a = el('input', attrs={'id': 'a'})
		input_b = el('input', attrs={'id': 'b'})
		table = el(
			'table',
			el(
				'thead',
				el(
					'tr',
					el('th'),
					el('th', tx('2024'), attrs={'scope': 'col', 'colspan': '2'}),
				),
			),
			el(
				'tbody',
				el(
					'tr',
					el('th', tx('Fruit'), attrs={'scope': 'row'}),
					el('td', input_a),
					el('td', input_b),
				),
			),
		)

		output = _serialize(table, interactive=[input_a, input_b])

		assert 'table_headers=2024 | Fruit' in _line_for(output, 'a')
		assert 'table_headers=2024 | Fruit' in _line_for(output, 'b')

	def test_colspan_zero_spans_to_last_column(self):
		input_a = el('input', attrs={'id': 'a'})
		input_b = el('input', attrs={'id': 'b'})
		table = el(
			'table',
			el('tr', el('th'), el('th', tx('All year'), attrs={'scope': 'col', 'colspan': '0'})),
			el(
				'tr',
				el('th', tx('Fruit'), attrs={'scope': 'row'}),
				el('td', input_a),
				el('td', input_b),
			),
		)

		output = _serialize(table, interactive=[input_a, input_b])

		assert 'table_headers=All year | Fruit' in _line_for(output, 'a')
		assert 'table_headers=All year | Fruit' in _line_for(output, 'b')

	def test_multi_level_column_headers_all_collected(self):
		input_a = el('input', attrs={'id': 'a'})
		input_b = el('input', attrs={'id': 'b'})
		table = el(
			'table',
			el('tr', el('th', tx('2024'), attrs={'scope': 'col', 'colspan': '2'})),
			el(
				'tr',
				el('th', tx('Q1'), attrs={'scope': 'col'}),
				el('th', tx('Q2'), attrs={'scope': 'col'}),
			),
			el('tr', el('td', input_a), el('td', input_b)),
		)

		output = _serialize(table, interactive=[input_a, input_b])

		assert 'table_headers=2024 | Q1' in _line_for(output, 'a')
		assert 'table_headers=2024 | Q2' in _line_for(output, 'b')

	def test_hierarchical_row_headers_all_collected(self):
		input_value = el('input', attrs={'id': 'count'})
		table = el(
			'table',
			el(
				'tbody',
				el(
					'tr',
					el('th', tx('Fruit'), attrs={'scope': 'row'}),
					el('th', tx('Apple'), attrs={'scope': 'row'}),
					el('td', input_value),
				),
			),
		)

		output = _serialize(table, interactive=[input_value])

		assert 'table_headers=Fruit | Apple' in _line_for(output, 'count')

	def test_headers_attribute_uses_only_valid_same_table_th_refs(self):
		input_value = el('input', attrs={'id': 'v'})
		table = el(
			'table',
			el(
				'tr',
				el('th', tx('Alpha'), attrs={'id': 'h-col', 'scope': 'col'}),
				el('th', tx('Beta'), attrs={'id': 'h-row', 'scope': 'row'}),
				# A data cell referenced by id must be ignored (only <th> counts).
				el('td', tx('Gamma'), attrs={'id': 'h-data'}),
				el(
					'td',
					input_value,
					attrs={'id': 'self-cell', 'headers': 'h-col h-row missing h-data self-cell'},
				),
			),
		)

		output = _serialize(table, interactive=[input_value])

		line = _line_for(output, 'v')
		assert 'table_headers=Alpha | Beta' in line
		assert 'Gamma' not in line

	def test_headers_attribute_follows_chained_references(self):
		input_value = el('input', attrs={'id': 'v'})
		table = el(
			'table',
			el(
				'tr',
				el('th', tx('Group'), attrs={'id': 'h-group'}),
				el('th', tx('Subhead'), attrs={'id': 'h-sub', 'headers': 'h-group'}),
				el('td', input_value, attrs={'headers': 'h-sub'}),
			),
		)

		output = _serialize(table, interactive=[input_value])

		line = _line_for(output, 'v')
		assert 'table_headers=Subhead | Group' in line

	def test_invalid_headers_attribute_has_no_scope_fallback(self):
		input_value = el('input', attrs={'id': 'v'})
		table = el(
			'table',
			el(
				'tr',
				el('th', tx('January'), attrs={'scope': 'col'}),
				el('td', input_value, attrs={'headers': 'ghost-ref'}),
			),
		)

		output = _serialize(table, interactive=[input_value])

		line = _line_for(output, 'v')
		assert 'table_headers' not in line

	def test_interactive_header_cell_with_headers_attribute(self):
		input_sort = el('input', attrs={'id': 'sort'})
		table = el(
			'table',
			el(
				'tr',
				el('th', tx('Label'), attrs={'id': 'h-label'}),
				el('th', input_sort, attrs={'headers': 'h-label'}),
			),
		)

		output = _serialize(table, interactive=[input_sort])

		assert 'table_headers=Label' in _line_for(output, 'sort')

	def test_nested_tables_are_associated_independently(self):
		inner_input = el('input', attrs={'id': 'inner'})
		inner_table = el(
			'table',
			el('thead', el('tr', el('th', tx('Inner col'), attrs={'scope': 'col'}))),
			el('tbody', el('tr', el('td', inner_input))),
		)
		outer_input = el('input', attrs={'id': 'outer'})
		outer_table = el(
			'table',
			el('thead', el('tr', el('th'), el('th', tx('Outer col'), attrs={'scope': 'col'}))),
			el(
				'tbody',
				el(
					'tr',
					el('th', tx('Outer row'), attrs={'scope': 'row'}),
					el('td', outer_input, inner_table),
				),
			),
		)

		output = _serialize(outer_table, interactive=[outer_input, inner_input])

		assert 'table_headers=Outer col | Outer row' in _line_for(output, 'outer')
		inner_line = _line_for(output, 'inner')
		assert 'table_headers=Inner col' in inner_line
		assert 'Outer' not in inner_line

	def test_headers_attribute_never_resolves_across_nested_tables(self):
		inner_input = el('input', attrs={'id': 'inner'})
		inner_table = el(
			'table',
			el(
				'tbody',
				el('tr', el('th', tx('Inner header'), attrs={'id': 'shared-id', 'scope': 'col'})),
				el('tr', el('td', inner_input)),
			),
		)
		outer_input = el('input', attrs={'id': 'outer'})
		outer_table = el(
			'table',
			el(
				'tbody',
				el(
					'tr',
					# The referenced id only exists on the nested table's th.
					el('td', outer_input, attrs={'headers': 'shared-id'}),
					el('td', inner_table),
				),
			),
		)

		output = _serialize(outer_table, interactive=[outer_input, inner_input])

		assert 'table_headers' not in _line_for(output, 'outer')
		assert 'table_headers=Inner header' in _line_for(output, 'inner')

	def test_no_guessing_without_explicit_scope_or_headers(self):
		input_value = el('input', attrs={'id': 'v'})
		table = el(
			'table',
			el('tr', el('th'), el('th', tx('January'))),
			el('tr', el('th', tx('Fruit')), el('td', input_value)),
		)

		output = _serialize(table, interactive=[input_value])

		assert 'table_headers' not in _line_for(output, 'v')

	def test_hidden_header_text_is_not_used(self):
		input_value = el('input', attrs={'id': 'v'})
		table = el(
			'table',
			el(
				'tr',
				el('th', tx('HIDDEN', visible=False), attrs={'scope': 'col'}),
				el('th', tx('Fruit'), attrs={'scope': 'row'}),
				el('td', input_value),
			),
		)

		output = _serialize(table, interactive=[input_value])

		line = _line_for(output, 'v')
		assert 'HIDDEN' not in line
		assert 'table_headers=Fruit' in line

	def test_paint_order_ignored_header_text_is_not_used(self):
		input_value = el('input', attrs={'id': 'v'})
		covered_text = tx('COVERED')
		table = el(
			'table',
			el(
				'tr',
				el('th', covered_text, attrs={'scope': 'col'}),
				el('th', tx('Fruit'), attrs={'scope': 'row'}),
				el('td', input_value),
			),
		)

		output = _serialize(table, interactive=[input_value], paint_ignored=[covered_text])

		line = _line_for(output, 'v')
		assert 'COVERED' not in line
		assert 'table_headers=Fruit' in line

	def test_explicitly_excluded_header_contributes_nothing_but_grid_stays_aligned(self):
		input_value = el('input', attrs={'id': 'v'})
		excluded_col_header = el('th', tx('EXCLUDED'), attrs={'scope': 'col'})
		table = el(
			'table',
			el(
				'thead',
				el('tr', el('th'), excluded_col_header),
			),
			el(
				'tbody',
				el(
					'tr',
					el('th', tx('Fruit'), attrs={'scope': 'row'}),
					el('td', input_value),
				),
			),
		)

		output = _serialize(table, interactive=[input_value], skip=[excluded_col_header])

		line = _line_for(output, 'v')
		assert 'EXCLUDED' not in line
		assert 'table_headers=Fruit' in line

	def test_elements_outside_tables_are_unchanged(self):
		input_value = el('input', attrs={'id': 'free'})
		root = el('div', input_value)

		output = _serialize(root, interactive=[input_value])

		line = _line_for(output, 'free')
		assert 'table_headers' not in line
		assert '[100]<input id=free />' in line

	def test_selector_indices_are_unchanged(self):
		table, input_a, input_b, input_c, input_d = _matrix_table()
		output = _serialize(table, interactive=[input_a, input_b, input_c, input_d])

		for element_id in ('a', 'b', 'c', 'd'):
			line = _line_for(output, element_id)
			assert f'id={element_id} ' in line
			assert line.count('[') == 1
