# @file purpose: Build synthetic interactive nodes for native HTML image maps
# (<img usemap="#..."> + <map><area></map>).
#
# <area> elements have no rendered box of their own, so the regular
# visibility/interactivity pipeline (which relies on snapshot bounds) cannot index
# them. This module turns every clickable area belonging to a visible main-document
# image into a synthetic EnhancedDOMTreeNode whose geometry follows the referencing
# image. Click-time coordinate conversion lives in the action watchdog and always
# uses the image's live layout, so scrolling/relayout is handled automatically.

from dataclasses import replace

from browser_use.dom.views import (
	EnhancedDOMTreeNode,
	ImageMapHotspot,
	NodeType,
)

# Safety cap: a single image referencing a map with thousands of areas must not
# blow up serialization.
MAX_AREAS_PER_IMAGE = 100

# Browsers historically accept several spellings for the area shape keyword.
_AREA_SHAPE_ALIASES = {
	'rect': 'rect',
	'rectangle': 'rect',
	'circle': 'circle',
	'circ': 'circle',
	'poly': 'poly',
	'polygon': 'poly',
}


def collect_main_document_maps(root_node: EnhancedDOMTreeNode, main_target_id: str) -> dict[str, EnhancedDOMTreeNode]:
	"""Index all <map> elements of the main document by their ``name`` and ``id``.

	Only nodes belonging to the main target are considered (image maps inside
	iframes/shadow trees are out of scope). First map in document order wins,
	matching browser resolution of ``usemap="#name"``.
	"""
	maps_by_name: dict[str, EnhancedDOMTreeNode] = {}

	def walk(node: EnhancedDOMTreeNode) -> None:
		for child in node.children_nodes or []:
			if child.target_id != main_target_id:
				continue
			if child.node_type == NodeType.ELEMENT_NODE and child.node_name == 'MAP':
				attributes = child.attributes or {}
				for key in (attributes.get('name'), attributes.get('id')):
					if key and key not in maps_by_name:
						maps_by_name[key] = child
			walk(child)

	walk(root_node)
	return maps_by_name


def iter_area_nodes(map_node: EnhancedDOMTreeNode):
	"""Yield <area> element descendants of a <map> in document order.

	Areas are normally direct children of the map, but we tolerate wrapper
	elements (e.g. <noscript>-unwrapped content) by searching descendants.
	"""

	def walk(node: EnhancedDOMTreeNode):
		for child in node.children_nodes or []:
			if child.node_type != NodeType.ELEMENT_NODE:
				continue
			if child.node_name == 'AREA':
				yield child
			else:
				yield from walk(child)

	yield from walk(map_node)


def _parse_area_coords(coords_attr: str) -> list[float] | None:
	"""Parse an area ``coords`` attribute into finite numbers.

	HTML coords are comma-separated lengths in the image's intrinsic pixel
	space. Any empty or non-numeric token makes the whole attribute invalid.
	"""
	tokens = coords_attr.split(',')
	numbers: list[float] = []
	for token in tokens:
		token = token.strip()
		if not token:
			return None
		try:
			value = float(token)
		except ValueError:
			return None
		if value != value or value in (float('inf'), float('-inf')):
			return None
		numbers.append(value)
	return numbers


def parse_area_hotspot_geometry(area_node: EnhancedDOMTreeNode) -> tuple[str, tuple[float, ...]] | None:
	"""Validate and normalize an area's shape/coords.

	Returns ``(shape, coords)`` with shape in {'rect', 'circle', 'poly'}, or
	``None`` when the area must not be indexed as an independent hotspot
	(unknown/default shape, missing or malformed coords, degenerate region).
	"""
	attributes = area_node.attributes or {}
	raw_shape = (attributes.get('shape') or '').strip().lower() or 'rect'
	shape = _AREA_SHAPE_ALIASES.get(raw_shape)
	if shape is None:
		# 'default' (whole image) and unknown shapes are not point-shaped targets.
		return None

	coords_attr = attributes.get('coords')
	if coords_attr is None:
		return None
	numbers = _parse_area_coords(coords_attr)
	if numbers is None:
		return None

	if shape == 'rect':
		# x1,y1,x2,y2 - extra values are ignored, reversed corners are normalized.
		if len(numbers) < 4:
			return None
		x1, y1, x2, y2 = numbers[:4]
		left, right = min(x1, x2), max(x1, x2)
		top, bottom = min(y1, y2), max(y1, y2)
		if right <= left or bottom <= top:
			return None
		return shape, (left, top, right, bottom)

	if shape == 'circle':
		# x,y,r
		if len(numbers) < 3:
			return None
		x, y, radius = numbers[:3]
		if radius <= 0:
			return None
		return shape, (x, y, radius)

	# poly: x1,y1,x2,y2,... - a trailing value without a partner is ignored,
	# and at least 3 points are required for a clickable polygon.
	pair_count = len(numbers) // 2
	if pair_count < 3:
		return None
	return shape, tuple(numbers[: pair_count * 2])


def is_area_explicitly_excluded(area_node: EnhancedDOMTreeNode, session_id: str | None) -> bool:
	"""Check the same exclude attributes the serializer honors for normal nodes."""
	attributes = area_node.attributes or {}
	if session_id:
		if (attributes.get(f'data-browser-use-exclude-{session_id}') or '').lower() == 'true':
			return True
	return (attributes.get('data-browser-use-exclude') or '').lower() == 'true'


def build_synthetic_area_node(
	area_node: EnhancedDOMTreeNode,
	image_node: EnhancedDOMTreeNode,
	shape: str,
	coords: tuple[float, ...],
) -> EnhancedDOMTreeNode:
	"""Create the synthetic selector node representing one image-map area.

	The node mirrors the area element (tag/attributes/backend id) but borrows the
	image's snapshot geometry so the normal visibility/bbox pipelines accept it.
	Paint-order data is intentionally dropped: areas of one image share identical
	bounds and must not paint-hide each other.
	"""
	snapshot = image_node.snapshot_node
	if snapshot is not None:
		snapshot = replace(snapshot, paint_order=None, stacking_contexts=None)

	return EnhancedDOMTreeNode(
		node_id=area_node.node_id,
		backend_node_id=area_node.backend_node_id,
		node_type=area_node.node_type,
		node_name=area_node.node_name,
		node_value=area_node.node_value,
		attributes=dict(area_node.attributes or {}),
		is_scrollable=False,
		frame_id=image_node.frame_id,
		session_id=image_node.session_id,
		target_id=image_node.target_id,
		content_document=None,
		shadow_root_type=None,
		shadow_roots=None,
		parent_node=image_node,
		children_nodes=[],
		ax_node=None,
		snapshot_node=snapshot,
		is_visible=True,
		has_js_click_listener=area_node.has_js_click_listener,
		absolute_position=None,
		image_map_hotspot=ImageMapHotspot(
			image_backend_node_id=image_node.backend_node_id,
			shape=shape,
			coords=coords,
		),
	)


def build_hotspot_nodes_for_image(
	image_node: EnhancedDOMTreeNode,
	maps_by_name: dict[str, EnhancedDOMTreeNode],
	session_id: str | None,
) -> list[EnhancedDOMTreeNode]:
	"""Build one synthetic clickable node per valid href-bearing area of the image's map."""
	attributes = image_node.attributes or {}
	usemap = (attributes.get('usemap') or '').strip()
	# Same-document fragment references only ("#map-name").
	if not usemap.startswith('#'):
		return []
	map_name = usemap[1:].strip()
	if not map_name:
		return []

	map_node = maps_by_name.get(map_name)
	if map_node is None:
		return []

	hotspot_nodes: list[EnhancedDOMTreeNode] = []
	for area_node in iter_area_nodes(map_node):
		if len(hotspot_nodes) >= MAX_AREAS_PER_IMAGE:
			break

		area_attributes = area_node.attributes or {}
		href = (area_attributes.get('href') or '').strip()
		# Only hyperlink areas are independent interaction targets.
		# 'nohref' is the obsolete attribute marking an inactive area.
		if not href or 'nohref' in area_attributes:
			continue
		if is_area_explicitly_excluded(area_node, session_id):
			continue

		geometry = parse_area_hotspot_geometry(area_node)
		if geometry is None:
			continue
		shape, coords = geometry

		hotspot_nodes.append(build_synthetic_area_node(area_node, image_node, shape, coords))

	return hotspot_nodes
