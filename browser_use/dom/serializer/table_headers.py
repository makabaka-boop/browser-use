# @file purpose: Annotates interactive elements inside native HTML table cells
# with the text of their associated row/column header cells, so the flattened
# LLM-facing DOM representation keeps parameter-matrix context (which row and
# column a control belongs to).

"""
Association rules (deliberately conservative - never guess):

1. A cell with a ``headers`` attribute only gets the referenced header cells
   that are ``<th>`` elements of the *same table* (duplicate IDs resolve to the
   first element, the cell itself is excluded, and references may chain through
   the referenced header's own ``headers`` attribute). Invalid references are
   ignored and there is no fallback to the ``scope`` algorithm.
2. Otherwise, a data cell (``<td>``) is associated with header cells that
   explicitly declare ``scope="row"`` / ``scope="col"``. A row header applies
   to every cell to its right in the spanned rows within the same row group;
   a column header applies to every cell below it in the spanned columns
   (across thead/tbody boundaries). ``rowspan`` / ``colspan`` (including the
   ``0`` "span to the end" form) are resolved via an explicit table grid.
3. Nested tables are processed independently. Header cells (``<th>``) without
   a ``headers`` attribute get no automatic association. When no header can be
   determined, nothing is added.

The header text reuses the existing representation rules: only visible text
nodes that are not removed by paint-order filtering are read, and cells that
disappeared from the simplified tree (e.g. ``data-browser-use-exclude``)
contribute nothing. Content of nested tables is never mixed into a header.
"""

from collections import deque
from dataclasses import dataclass

from browser_use.dom.utils import cap_text_length
from browser_use.dom.views import EnhancedDOMTreeNode, NodeType, SimplifiedNode

TABLE_TAG = 'table'
SECTION_TAGS = ('thead', 'tbody', 'tfoot')
ROW_TAG = 'tr'
CELL_TAGS = ('td', 'th')
HEADER_TAG = 'th'
SCOPE_ROW = 'row'
SCOPE_COL = 'col'

MAX_TABLE_HEADERS_TEXT_LENGTH = 200
HEADER_JOINER = ' | '


@dataclass(slots=True)
class _CellBox:
	"""A cell placed on the table grid (inclusive row/column coordinates)."""

	cell: EnhancedDOMTreeNode
	r0: int
	c0: int
	r1: int
	c1: int
	section: EnhancedDOMTreeNode | None
	is_header: bool


def annotate_table_headers(
	enhanced_root: EnhancedDOMTreeNode | None,
	simplified_root: SimplifiedNode | None,
) -> None:
	"""Annotate interactive simplified nodes with their table header text.

	Args:
		enhanced_root: Full enhanced DOM tree, used for table structure (it keeps
			hidden/excluded elements so grid alignment stays correct).
		simplified_root: Filtered simplified tree that is serialized for the LLM;
			interactive nodes in it receive ``table_headers`` in place.
	"""
	if enhanced_root is None or simplified_root is None:
		return

	cell_header_map = _build_cell_header_map(enhanced_root)
	if not cell_header_map:
		return

	# Map each surviving enhanced element to its simplified node, so header text
	# can be extracted with the same visibility/paint-order/exclusion rules that
	# govern the serialized representation.
	simplified_by_id: dict[int, SimplifiedNode] = {}
	_index_simplified_tree(simplified_root, simplified_by_id)

	header_text_cache: dict[int, str] = {}

	def resolve_header_text(header_cell: EnhancedDOMTreeNode) -> str:
		key = id(header_cell)
		if key not in header_text_cache:
			simplified_cell = simplified_by_id.get(key)
			text = _extract_visible_header_text(simplified_cell) if simplified_cell is not None else ''
			header_text_cache[key] = text
		return header_text_cache[key]

	def annotate(node: SimplifiedNode) -> None:
		if node.is_interactive:
			enclosing_cell = _find_enclosing_cell(node.original_node)
			if enclosing_cell is not None:
				header_cells = cell_header_map.get(id(enclosing_cell))
				if header_cells:
					texts = [resolve_header_text(header_cell) for header_cell in header_cells]
					texts = [text for text in texts if text]
					if texts:
						node.table_headers = cap_text_length(
							HEADER_JOINER.join(texts),
							MAX_TABLE_HEADERS_TEXT_LENGTH,
						)
		for child in node.children:
			annotate(child)

	annotate(simplified_root)


# ---------------------------------------------------------------------------
# Table structure
# ---------------------------------------------------------------------------


def _build_cell_header_map(enhanced_root: EnhancedDOMTreeNode) -> dict[int, list[EnhancedDOMTreeNode]]:
	"""Map every cell node to the ordered header cells associated with it."""
	tables = _collect_tables(enhanced_root)
	cell_header_map: dict[int, list[EnhancedDOMTreeNode]] = {}
	for table in tables:
		cell_header_map.update(_build_map_for_table(table))
	return cell_header_map


def _collect_tables(root: EnhancedDOMTreeNode) -> list[EnhancedDOMTreeNode]:
	"""Collect every table element, including nested tables and tables in
	shadow roots or iframe documents; each table is processed independently."""
	tables: list[EnhancedDOMTreeNode] = []
	stack = [root]
	while stack:
		node = stack.pop()
		if node.node_type == NodeType.ELEMENT_NODE and node.tag_name == TABLE_TAG:
			tables.append(node)
		stack.extend(node.children_nodes or [])
		stack.extend(node.shadow_roots or [])
		if node.content_document is not None:
			stack.append(node.content_document)
	return tables


def _build_map_for_table(table: EnhancedDOMTreeNode) -> dict[int, list[EnhancedDOMTreeNode]]:
	"""Build the cell -> headers map for one table element."""
	rows = _collect_rows(table)
	grid, boxes = _build_grid(rows)
	if not boxes:
		return {}

	# IDs only resolve to <th> cells that belong to this same table.
	header_by_id: dict[str, _CellBox] = {}
	for box in boxes:
		if box.is_header:
			element_id = (box.cell.attributes or {}).get('id')
			if element_id and element_id not in header_by_id:
				header_by_id[element_id] = box

	cell_header_map: dict[int, list[EnhancedDOMTreeNode]] = {}
	sections = [section for section, _ in rows]
	for box in boxes:
		headers_attribute = (box.cell.attributes or {}).get('headers')
		if headers_attribute is not None and headers_attribute.strip():
			header_boxes = _resolve_headers_attribute(headers_attribute, box, header_by_id)
		elif not box.is_header:
			header_boxes = _resolve_scope_headers(box, grid, sections)
		else:
			header_boxes = []

		if header_boxes:
			cell_header_map[id(box.cell)] = [header_box.cell for header_box in header_boxes]

	return cell_header_map


def _collect_rows(
	table: EnhancedDOMTreeNode,
) -> list[tuple[EnhancedDOMTreeNode | None, list[EnhancedDOMTreeNode]]]:
	"""Collect ``(section, cells)`` rows in DOM order.

	Only the strict table hierarchy is inspected (table > thead/tbody/tfoot >
	tr > td/th), so rows and cells of nested tables are never picked up.
	"""
	rows: list[tuple[EnhancedDOMTreeNode | None, list[EnhancedDOMTreeNode]]] = []
	for child in table.children:
		if child.node_type != NodeType.ELEMENT_NODE:
			continue
		tag = child.node_name.lower()
		if tag in SECTION_TAGS:
			for row in child.children:
				if row.node_type == NodeType.ELEMENT_NODE and row.node_name.lower() == ROW_TAG:
					rows.append((child, _row_cells(row)))
		elif tag == ROW_TAG:
			rows.append((None, _row_cells(child)))
	return rows


def _row_cells(row: EnhancedDOMTreeNode) -> list[EnhancedDOMTreeNode]:
	return [child for child in row.children if child.node_type == NodeType.ELEMENT_NODE and child.node_name.lower() in CELL_TAGS]


def _build_grid(
	rows: list[tuple[EnhancedDOMTreeNode | None, list[EnhancedDOMTreeNode]]],
) -> tuple[list[list[_CellBox | None]], list[_CellBox]]:
	"""Place cells on a 2D grid honoring rowspan/colspan occupancy."""
	grid: list[list[_CellBox | None]] = []
	boxes: list[_CellBox] = []
	zero_column_span: list[_CellBox] = []
	zero_row_span: list[_CellBox] = []

	for r, (section, cells) in enumerate(rows):
		while len(grid) <= r:
			grid.append([])
		c = 0
		for cell in cells:
			# Skip grid positions occupied by earlier rowspan cells.
			while c < len(grid[r]) and grid[r][c] is not None:
				c += 1
			rowspan = _span(cell, 'rowspan')
			colspan = _span(cell, 'colspan')
			box = _CellBox(
				cell=cell,
				r0=r,
				c0=c,
				r1=r + (rowspan if rowspan > 0 else 1) - 1,
				c1=c + (colspan if colspan > 0 else 1) - 1,
				section=section,
				is_header=cell.node_name.lower() == HEADER_TAG,
			)
			boxes.append(box)
			if colspan == 0:
				zero_column_span.append(box)
			if rowspan == 0:
				zero_row_span.append(box)
			_occupy(grid, box)
			c = box.c1 + 1

	if not grid:
		return grid, boxes

	# span="0" means "span to the end of the table" (columns first, then rows).
	column_count = max(len(row) for row in grid)
	for box in zero_column_span:
		if column_count - 1 > box.c1:
			box.c1 = column_count - 1
			_occupy(grid, box)
	row_count = len(grid)
	for box in zero_row_span:
		if row_count - 1 > box.r1:
			box.r1 = row_count - 1
			_occupy(grid, box)

	for row in grid:
		if len(row) < column_count:
			row.extend([None] * (column_count - len(row)))

	return grid, boxes


def _occupy(grid: list[list[_CellBox | None]], box: _CellBox) -> None:
	for r in range(box.r0, box.r1 + 1):
		while len(grid) <= r:
			grid.append([])
		row = grid[r]
		if len(row) <= box.c1:
			row.extend([None] * (box.c1 + 1 - len(row)))
		for c in range(box.c0, box.c1 + 1):
			row[c] = box


def _span(cell: EnhancedDOMTreeNode, attribute: str) -> int:
	"""Parse rowspan/colspan: non-positive/garbage values default to 1,
	except 0 which is the valid "span to the end" marker."""
	raw_value = (cell.attributes or {}).get(attribute)
	if raw_value is None:
		return 1
	try:
		value = int(str(raw_value).strip())
	except (ValueError, TypeError):
		return 1
	return value if value >= 0 else 1


# ---------------------------------------------------------------------------
# Header resolution
# ---------------------------------------------------------------------------


def _resolve_headers_attribute(
	headers_attribute: str,
	target: _CellBox,
	header_by_id: dict[str, _CellBox],
) -> list[_CellBox]:
	"""Resolve a cell's headers attribute to same-table <th> boxes.

	References may chain through a referenced header's own headers attribute;
	cycles and duplicates are skipped. There is no scope fallback.
	"""
	resolved: list[_CellBox] = []
	seen: set[int] = set()
	pending: deque[str] = deque(headers_attribute.split())
	while pending:
		token = pending.popleft()
		referenced = header_by_id.get(token)
		if referenced is None or referenced is target:
			continue
		key = id(referenced.cell)
		if key in seen:
			continue
		seen.add(key)
		resolved.append(referenced)
		chained = (referenced.cell.attributes or {}).get('headers')
		if chained:
			pending.extend(chained.split())
	return resolved


def _scope_of(box: _CellBox) -> str | None:
	return ((box.cell.attributes or {}).get('scope') or '').strip().lower() or None


def _resolve_scope_headers(
	target: _CellBox,
	grid: list[list[_CellBox | None]],
	sections: list[EnhancedDOMTreeNode | None],
) -> list[_CellBox]:
	"""Resolve explicit scope=col / scope=row headers for a data cell.

	``scope="col"`` declares a header for every cell below it in the column
	(across thead/tbody boundaries), ``scope="row"`` for every cell to its
	right within the same row group - intervening data cells do not break the
	association. Only explicit scopes are used; headers without ``scope`` are
	never inferred.
	"""
	column_headers: dict[int, _CellBox] = {}
	# Column direction: scan upwards in every column the cell spans.
	for c in range(target.c0, target.c1 + 1):
		r = target.r0 - 1
		while r >= 0:
			referenced = grid[r][c]
			if referenced is not None and referenced.is_header and _scope_of(referenced) == SCOPE_COL:
				column_headers.setdefault(id(referenced.cell), referenced)
				# Jump past the whole header (it may span multiple rows).
				r = referenced.r0 - 1
			else:
				r -= 1

	row_headers: dict[int, _CellBox] = {}
	# Row direction: scan leftwards in every row the cell spans.
	for r in range(target.r0, target.r1 + 1):
		target_section = sections[r] if r < len(sections) else None
		c = target.c0 - 1
		while c >= 0:
			referenced = grid[r][c]
			if (
				referenced is not None
				and referenced.is_header
				and _scope_of(referenced) == SCOPE_ROW
				and referenced.section is target_section
			):
				row_headers.setdefault(id(referenced.cell), referenced)
				# Jump past the whole header (it may span multiple columns).
				c = referenced.c0 - 1
			else:
				c -= 1

	ordered = sorted(column_headers.values(), key=lambda box: (box.r0, box.c0))
	ordered.extend(sorted(row_headers.values(), key=lambda box: (box.r0, box.c0)))
	return ordered


# ---------------------------------------------------------------------------
# Simplified tree indexing & text extraction
# ---------------------------------------------------------------------------


def _index_simplified_tree(node: SimplifiedNode, index: dict[int, SimplifiedNode]) -> None:
	index[id(node.original_node)] = node
	for child in node.children:
		_index_simplified_tree(child, index)


def _find_enclosing_cell(node: EnhancedDOMTreeNode) -> EnhancedDOMTreeNode | None:
	"""Nearest td/th ancestor, stopping at document boundaries.

	Shadow roots are transparent (a control inside a shadow tree hosted in a
	cell still belongs to that cell), while iframe documents never leak header
	context from the outer document.
	"""
	current = node.parent_node
	while current is not None:
		if current.node_type == NodeType.DOCUMENT_NODE:
			return None
		if current.node_type == NodeType.ELEMENT_NODE and current.node_name.lower() in CELL_TAGS:
			return current
		current = current.parent_node
	return None


def _extract_visible_header_text(cell_node: SimplifiedNode | None) -> str:
	"""Collect a header cell's visible text exactly like the serializer does.

	- only TEXT_NODEs that are visible and not ignored by paint order;
	- descendants of nested tables are excluded (nested tables are associated
	  independently);
	- cells removed from the simplified tree contribute nothing.
	"""
	if cell_node is None:
		return ''

	parts: list[str] = []

	def walk(node: SimplifiedNode) -> None:
		original = node.original_node
		if original.node_type == NodeType.ELEMENT_NODE and original.tag_name == TABLE_TAG:
			return
		if original.node_type == NodeType.TEXT_NODE:
			is_visible = original.snapshot_node and original.is_visible
			if is_visible and not node.ignored_by_paint_order:
				text = ' '.join((original.node_value or '').split()).strip()
				if len(text) > 1:
					parts.append(text)
		for child in node.children:
			walk(child)

	walk(cell_node)
	return ' '.join(parts)
