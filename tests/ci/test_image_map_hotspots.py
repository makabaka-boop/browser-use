"""Unit tests for native HTML image map (<img usemap> + <map><area>) indexing.

Covers:
- rect/circle/poly areas with href become independent interactive targets;
- alt/title text is exposed in the serialized representation;
- invalid coordinates, no-href/nohref areas are not indexed;
- hidden or explicitly excluded images/areas are not indexed;
- the synthetic targets carry validated geometry bound to the image's backend id.
"""

from browser_use.dom.serializer.image_maps import parse_area_hotspot_geometry
from browser_use.dom.serializer.serializer import DOMTreeSerializer
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, EnhancedSnapshotNode, NodeType

_TARGET = 'main-target'


def _snapshot(bounds=(0, 0, 300, 200), visible=True):
	return EnhancedSnapshotNode(
		is_clickable=None,
		cursor_style='auto',
		bounds=DOMRect(x=bounds[0], y=bounds[1], width=bounds[2], height=bounds[3]),
		clientRects=DOMRect(x=bounds[0], y=bounds[1], width=bounds[2], height=bounds[3]),
		scrollRects=None,
		computed_styles={
			'display': 'block',
			'visibility': 'visible',
			'opacity': '1',
			'background-color': 'rgba(0, 0, 0, 0)',
			'overflow': 'visible',
		},
		paint_order=1,
		stacking_contexts=0,
	)


def _element(tag, node_id, *, attributes=None, is_visible=True, snapshot=None, children=None):
	return EnhancedDOMTreeNode(
		node_id=node_id,
		backend_node_id=node_id,
		node_type=NodeType.ELEMENT_NODE,
		node_name=tag.upper(),
		node_value='',
		attributes=attributes or {},
		is_scrollable=False,
		is_visible=is_visible,
		absolute_position=None,
		target_id=_TARGET,
		frame_id=None,
		session_id=None,
		content_document=None,
		shadow_root_type=None,
		shadow_roots=None,
		parent_node=None,
		children_nodes=children or [],
		ax_node=None,
		snapshot_node=snapshot if snapshot is not None else (_snapshot() if is_visible and tag == 'img' else None),
	)


def _area(node_id, *, shape, coords, href: str | None = '#x', alt=None, title=None, extra_attrs=None, is_visible=False):
	attributes = {'shape': shape, 'coords': coords}
	if href is not None:
		attributes['href'] = href
	if alt is not None:
		attributes['alt'] = alt
	if title is not None:
		attributes['title'] = title
	if extra_attrs:
		attributes.update(extra_attrs)
	return _element('area', node_id, attributes=attributes, is_visible=is_visible, snapshot=None)


def _build_tree():
	img = _element(
		'img',
		3,
		attributes={'usemap': '#main-map', 'alt': 'diagram', 'src': 'diagram.png'},
	)
	valid_rect = _area(10, shape='rect', coords='0,0,100,100', href='#rect', alt='Rect hotspot')
	valid_circle = _area(11, shape='circle', coords='200,50,40', href='#circle', alt='Circle hotspot')
	valid_poly = _area(12, shape='poly', coords='150,200,200,120,250,200', href='#poly', title='Polygon hotspot')
	bad_coords = _area(13, shape='rect', coords='1,2,oops', href='#bad', alt='Bad coords')
	few_points = _area(14, shape='poly', coords='0,0,10,10', href='#few', alt='Few points')
	zero_rect = _area(15, shape='rect', coords='5,5,5,5', href='#zero', alt='Zero area')
	no_href = _area(16, shape='rect', coords='0,0,5,5', href=None, alt='No href')
	nohref = _area(
		17,
		shape='rect',
		coords='0,0,5,5',
		href='#nohref-target',
		alt='nohref attr',
		extra_attrs={'nohref': ''},
	)
	image_map = _element(
		'map',
		20,
		attributes={'name': 'main-map'},
		is_visible=False,
		snapshot=None,
		children=[valid_rect, valid_circle, valid_poly, bad_coords, few_points, zero_rect, no_href, nohref],
	)
	assert image_map.children_nodes is not None
	for area in image_map.children_nodes:
		area.parent_node = image_map

	hidden_img = _element(
		'img',
		30,
		attributes={'usemap': '#hidden-map', 'alt': 'hidden diagram'},
		is_visible=False,
		snapshot=None,
	)
	hidden_area = _area(31, shape='rect', coords='0,0,10,10', href='#hidden', alt='Hidden hotspot')
	hidden_map = _element(
		'map',
		32,
		attributes={'name': 'hidden-map'},
		is_visible=False,
		snapshot=None,
		children=[hidden_area],
	)
	hidden_area.parent_node = hidden_map

	excluded_img = _element(
		'img',
		40,
		attributes={'usemap': '#excluded-map', 'data-browser-use-exclude': 'true'},
	)
	excluded_area = _area(41, shape='rect', coords='0,0,10,10', href='#excluded', alt='Excluded hotspot')
	excluded_map = _element(
		'map',
		42,
		attributes={'name': 'excluded-map'},
		is_visible=False,
		snapshot=None,
		children=[excluded_area],
	)
	excluded_area.parent_node = excluded_map

	body = _element(
		'body',
		2,
		is_visible=True,
		snapshot=_snapshot((0, 0, 1920, 1000)),
		children=[img, image_map, hidden_img, hidden_map, excluded_img, excluded_map],
	)
	root = _element('html', 1, is_visible=True, snapshot=_snapshot((0, 0, 1920, 1000)), children=[body])
	assert body.children_nodes is not None
	for child in body.children_nodes:
		child.parent_node = body
	body.parent_node = root
	return root, {
		'rect': valid_rect,
		'circle': valid_circle,
		'poly': valid_poly,
		'hidden_img': hidden_img,
	}


class TestAreaCoordsParsing:
	def test_valid_shapes(self):
		assert parse_area_hotspot_geometry(_area(1, shape='rect', coords='1,2,11,12')) == ('rect', (1.0, 2.0, 11.0, 12.0))
		assert parse_area_hotspot_geometry(_area(2, shape='circle', coords='5,6,3')) == ('circle', (5.0, 6.0, 3.0))
		assert parse_area_hotspot_geometry(_area(3, shape='poly', coords='0,0,4,0,2,3')) == (
			'poly',
			(0.0, 0.0, 4.0, 0.0, 2.0, 3.0),
		)

	def test_shape_aliases(self):
		rect_alias = parse_area_hotspot_geometry(_area(1, shape='rectangle', coords='0,0,1,1'))
		circle_alias = parse_area_hotspot_geometry(_area(2, shape='circ', coords='0,0,1'))
		poly_alias = parse_area_hotspot_geometry(_area(3, shape='polygon', coords='0,0,1,0,0,1'))
		assert rect_alias is not None and rect_alias[0] == 'rect'
		assert circle_alias is not None and circle_alias[0] == 'circle'
		assert poly_alias is not None and poly_alias[0] == 'poly'

	def test_default_shape_is_rect(self):
		assert parse_area_hotspot_geometry(_area(1, shape='', coords='0,0,1,1')) == ('rect', (0.0, 0.0, 1.0, 1.0))

	def test_reversed_rect_corners_are_normalized(self):
		assert parse_area_hotspot_geometry(_area(1, shape='rect', coords='10,10,0,0')) == ('rect', (0.0, 0.0, 10.0, 10.0))

	def test_invalid_coords_rejected(self):
		assert parse_area_hotspot_geometry(_area(1, shape='rect', coords='0,0,x')) is None
		assert parse_area_hotspot_geometry(_area(2, shape='rect', coords='0,0,1')) is None  # too few
		assert parse_area_hotspot_geometry(_area(3, shape='rect', coords='5,5,5,6')) is None  # zero width
		assert parse_area_hotspot_geometry(_area(4, shape='circle', coords='1,1,0')) is None  # zero radius
		assert parse_area_hotspot_geometry(_area(5, shape='circle', coords='1,1,-3')) is None  # negative radius
		assert parse_area_hotspot_geometry(_area(6, shape='poly', coords='0,0,1,1')) is None  # < 3 points
		assert parse_area_hotspot_geometry(_area(7, shape='default', coords='')) is None  # whole-image default
		assert parse_area_hotspot_geometry(_area(8, shape='weird', coords='0,0,1,1')) is None

		area_missing_coords = _area(9, shape='rect', coords='0,0,1,1')
		area_missing_coords.attributes.pop('coords')
		assert parse_area_hotspot_geometry(area_missing_coords) is None


class TestImageMapIndexing:
	def test_only_valid_href_areas_are_indexed(self):
		root, nodes = _build_tree()
		state, _ = DOMTreeSerializer(root).serialize_accessible_elements()

		area_entries = {idx: node for idx, node in state.selector_map.items() if node.tag_name == 'area'}
		assert len(area_entries) == 3
		assert {node.backend_node_id for node in area_entries.values()} == {
			nodes['rect'].backend_node_id,
			nodes['circle'].backend_node_id,
			nodes['poly'].backend_node_id,
		}

		text = state.llm_representation()
		assert '<area' in text
		assert 'Rect hotspot' in text
		assert 'Circle hotspot' in text
		assert 'Polygon hotspot' in text
		# Invalid / hidden / excluded content must never be exposed.
		assert 'Bad coords' not in text
		assert 'Few points' not in text
		assert 'Zero area' not in text
		assert 'No href' not in text
		assert 'Hidden hotspot' not in text
		assert 'Excluded hotspot' not in text

	def test_hotspot_metadata_points_to_the_image_with_natural_pixel_coords(self):
		root, nodes = _build_tree()
		state, _ = DOMTreeSerializer(root).serialize_accessible_elements()

		indexed = {node.backend_node_id: node for node in state.selector_map.values() if node.tag_name == 'area'}
		rect_node = indexed[nodes['rect'].backend_node_id]
		assert rect_node.image_map_hotspot is not None
		assert rect_node.image_map_hotspot.image_backend_node_id == 3
		assert rect_node.image_map_hotspot.shape == 'rect'
		assert rect_node.image_map_hotspot.coords == (0.0, 0.0, 100.0, 100.0)
		# The synthetic node follows the image's frame/session.
		assert rect_node.session_id is None
		assert rect_node.parent_node is not None
		assert rect_node.parent_node.backend_node_id == 3

	def test_hidden_image_produces_no_hotspots(self):
		root, _ = _build_tree()
		state, _ = DOMTreeSerializer(root).serialize_accessible_elements()
		assert not any(
			node.image_map_hotspot and node.image_map_hotspot.image_backend_node_id == 30 for node in state.selector_map.values()
		)

	def test_session_specific_area_exclusion(self):
		root, _ = _build_tree()
		state, _ = DOMTreeSerializer(root, session_id='session-xyz').serialize_accessible_elements()
		assert len([n for n in state.selector_map.values() if n.tag_name == 'area']) == 3

		# Add a session-specific exclusion marker on the rect area and re-serialize.
		root, _ = _build_tree()
		html_children = root.children_nodes
		assert html_children is not None
		body_node = html_children[0]
		assert body_node.children_nodes is not None
		map_node = next(child for child in body_node.children_nodes if child.node_name == 'MAP')
		assert map_node.children_nodes is not None
		rect_area = next(a for a in map_node.children_nodes if a.attributes.get('alt') == 'Rect hotspot')
		rect_area.attributes['data-browser-use-exclude-session-xyz'] = 'true'
		state, _ = DOMTreeSerializer(root, session_id='session-xyz').serialize_accessible_elements()
		labels = {
			(n.attributes.get('alt') or n.attributes.get('title')) for n in state.selector_map.values() if n.tag_name == 'area'
		}
		assert labels == {'Circle hotspot', 'Polygon hotspot'}
