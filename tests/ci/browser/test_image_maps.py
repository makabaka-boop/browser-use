"""
Integration tests for native HTML image map (<img usemap> + <map><area>) support.

Verifies end-to-end with a real (headless) browser:
1. Valid rect/circle/poly href areas get independent clickable indices and show alt/title.
2. Invalid coords, no-href/default areas, hidden and excluded images are not indexed.
3. Clicks dispatch REAL mouse events at points inside the hotspot (after live
   coordinate conversion with a non-uniformly scaled image and scroll-into-view),
   triggering the page's native area behavior (anchor navigation / onclick).
4. When the image is hidden after indexing, clicking the cached hotspot index fails
   explicitly instead of clicking the image center or opening the href via JS.
5. Ordinary links keep working through the regular click path.

Usage:
	uv run pytest tests/ci/browser/test_image_maps.py -v -s
"""

import asyncio
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserSession
from browser_use.browser.profile import BrowserProfile, ViewportSize

VALID_HOTSPOTS = {
	'Rect region': 'rect',
	'Circle region': 'circle',
	'Polygon region': 'poly',
	'Scaled rect region': 'rect',
	'Scaled circle region': 'circle',
}

UNINDEXED_LABELS = [
	'Malformed region',
	'Two point region',
	'Zero region',
	'Default region',
	'No link region',
	'Hidden region',
	'Excluded region',
]

# Reports whether the last captured mousedown landed inside the expected natural-pixel region.
# Uses pageX/pageY against the image's document-absolute position so anchor-navigation
# scrolls after the click do not invalidate the measurement.
_LAST_DOWN_INSIDE_JS = """
(function(imageId, shape) {
	var downs = window.__mouseDowns;
	if (!downs.length) return {ok: false, reason: 'no mousedown recorded'};
	var d = downs[downs.length - 1];
	var img = document.getElementById(imageId);
	var r = img.getBoundingClientRect();
	var imgPageX = r.left + window.scrollX;
	var imgPageY = r.top + window.scrollY;
	var x = ((d.pageX - imgPageX) / r.width) * img.naturalWidth;
	var y = ((d.pageY - imgPageY) / r.height) * img.naturalHeight;
	var inside = false;
	if (shape === 'rect') {
		inside = x >= 0 && x <= 100 && y >= 0 && y <= 100;
	} else if (shape === 'circle') {
		var dx = x - 200, dy = y - 50;
		inside = dx * dx + dy * dy <= 40 * 40;
	} else if (shape === 'poly') {
		function sign(px, py, ax, ay, bx, by) {
			return (px - bx) * (ay - by) - (ax - bx) * (py - by);
		}
		var d1 = sign(x, y, 150, 200, 200, 120);
		var d2 = sign(x, y, 200, 120, 250, 200);
		var d3 = sign(x, y, 250, 200, 150, 200);
		var hasNeg = d1 < 0 || d2 < 0 || d3 < 0;
		var hasPos = d1 > 0 || d2 > 0 || d3 > 0;
		inside = !(hasNeg && hasPos);
	} else if (shape === 'scaled-rect') {
		inside = x >= 0 && x <= 50 && y >= 0 && y <= 50;
	}
	return {ok: true, inside: inside, tag: d.tag, label: d.label, natX: x, natY: y};
})
"""


@pytest.fixture(scope='module')
def http_server():
	"""Serve the image-map test page over HTTP."""
	server = HTTPServer()
	server.start()
	page_html = (Path(__file__).parent / 'test_image_maps_template.html').read_text()
	server.expect_request('/image-maps').respond_with_data(page_html, content_type='text/html')
	yield server
	server.stop()


@pytest.fixture(scope='module')
def base_url(http_server):
	return f'http://{http_server.host}:{http_server.port}'


@pytest.fixture(scope='function')
async def browser_session():
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
			keep_alive=True,
			window_size=ViewportSize(width=1920, height=1400),
		)
	)
	await session.start()
	yield session
	await session.kill()


def _area_index_by_label(selector_map, label):
	for idx, node in selector_map.items():
		if node.tag_name != 'area':
			continue
		if (node.attributes.get('alt') or node.attributes.get('title')) == label:
			return idx
	return None


async def _open_page(tools, browser_session, base_url):
	await tools.navigate(url=f'{base_url}/image-maps', new_tab=False, browser_session=browser_session)
	await asyncio.sleep(1.0)


async def _evaluate(browser_session, expression):
	cdp_session = await browser_session.get_or_create_cdp_session()
	result = await cdp_session.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True},
		session_id=cdp_session.session_id,
	)
	return result.get('result', {}).get('value')


class TestImageMaps:
	"""End-to-end image-map indexing and clicking behavior."""

	async def test_valid_href_areas_are_indexed_with_alt_or_title(self, browser_session, base_url):
		from browser_use.tools.service import Tools

		tools = Tools()
		await tools.navigate(url=f'{base_url}/image-maps', new_tab=False, browser_session=browser_session)
		await asyncio.sleep(1.0)

		state = await browser_session.get_browser_state_summary(include_screenshot=False, include_recent_events=False)
		assert state is not None and state.dom_state is not None
		selector_map = state.dom_state.selector_map
		text = state.dom_state.llm_representation()
		print(text)

		area_nodes = [node for node in selector_map.values() if node.tag_name == 'area']
		assert len(area_nodes) == len(VALID_HOTSPOTS), (
			f'expected exactly {len(VALID_HOTSPOTS)} indexed areas, got {len(area_nodes)}'
		)

		labels = {(node.attributes.get('alt') or node.attributes.get('title')) for node in area_nodes}
		assert labels == set(VALID_HOTSPOTS)

		for label in VALID_HOTSPOTS:
			assert label in text, f'area label {label!r} must be visible to the model'
		for label in UNINDEXED_LABELS:
			assert label not in text, f'invalid/hidden area {label!r} must not be indexed'

		# Every synthetic target carries validated geometry pointing at its image.
		main_image_id = await _evaluate(browser_session, "document.getElementById('main-image') === null ? null : 1")
		assert main_image_id == 1
		for node in area_nodes:
			assert node.image_map_hotspot is not None
			assert node.image_map_hotspot.shape == VALID_HOTSPOTS[node.attributes.get('alt') or node.attributes.get('title')]
			assert node.image_map_hotspot.image_backend_node_id > 0

	async def test_rect_circle_poly_clicks_hit_inside_hotspots_and_trigger_native_behavior(self, browser_session, base_url):
		from browser_use.tools.service import Tools

		tools = Tools()
		await _open_page(tools, browser_session, base_url)

		state = await browser_session.get_browser_state_summary(include_screenshot=False, include_recent_events=False)
		selector_map = state.dom_state.selector_map

		# --- rect area: native anchor navigation to #rect-anchor ----------------
		rect_index = _area_index_by_label(selector_map, 'Rect region')
		assert rect_index is not None
		rect_result = await tools.click(index=rect_index, browser_session=browser_session)
		assert not rect_result.error, f'rect click failed: {rect_result.error}'
		await asyncio.sleep(0.3)
		assert await _evaluate(browser_session, 'window.location.hash') == '#rect-anchor'

		rect_hit = await _evaluate(browser_session, _LAST_DOWN_INSIDE_JS + "('main-image', 'rect')")
		assert rect_hit['ok'] and rect_hit['inside'], f'rect click landed outside: {rect_hit}'
		assert rect_hit['tag'] == 'AREA'
		assert rect_hit['label'] == 'Rect region'

		# --- poly area: onclick runs and suppresses navigation ------------------
		poly_index = _area_index_by_label(selector_map, 'Polygon region')
		poly_result = await tools.click(index=poly_index, browser_session=browser_session)
		assert not poly_result.error, f'poly click failed: {poly_result.error}'
		await asyncio.sleep(0.3)
		assert await _evaluate(browser_session, 'window.location.hash') == '#rect-anchor', (
			'poly onclick returns false, hash must not change'
		)
		assert await _evaluate(browser_session, 'window.__polyClicks || 0') == 1
		poly_hit = await _evaluate(browser_session, _LAST_DOWN_INSIDE_JS + "('main-image', 'poly')")
		assert poly_hit['ok'] and poly_hit['inside'], f'poly click landed outside: {poly_hit}'
		assert poly_hit['tag'] == 'AREA'

		# --- circle area (title only, image re-scrolled into view) -------------
		circle_index = _area_index_by_label(selector_map, 'Circle region')
		circle_result = await tools.click(index=circle_index, browser_session=browser_session)
		assert not circle_result.error, f'circle click failed: {circle_result.error}'
		await asyncio.sleep(0.3)
		assert await _evaluate(browser_session, 'window.location.hash') == '#circle-anchor'
		circle_hit = await _evaluate(browser_session, _LAST_DOWN_INSIDE_JS + "('main-image', 'circle')")
		assert circle_hit['ok'] and circle_hit['inside'], f'circle click landed outside: {circle_hit}'
		assert circle_hit['tag'] == 'AREA'

		# --- 2x scaled image: rect area is hit at the scaled position ----------
		scaled_rect_index = _area_index_by_label(selector_map, 'Scaled rect region')
		assert scaled_rect_index is not None
		scaled_rect_result = await tools.click(index=scaled_rect_index, browser_session=browser_session)
		assert not scaled_rect_result.error, f'scaled rect click failed: {scaled_rect_result.error}'
		await asyncio.sleep(0.3)
		assert await _evaluate(browser_session, 'window.location.hash') == '#scaled-rect-anchor'
		scaled_hit = await _evaluate(browser_session, _LAST_DOWN_INSIDE_JS + "('scaled-image', 'scaled-rect')")
		assert scaled_hit['ok'] and scaled_hit['inside'], f'scaled rect click landed outside: {scaled_hit}'
		assert scaled_hit['tag'] == 'AREA'

		# --- 2x scaled circle: browsers do not scale circle hit regions, so the
		# visual hotspot is not natively reachable; we must fail explicitly and
		# never click anywhere else or open the href programmatically. ----------
		hash_before = await _evaluate(browser_session, 'window.location.hash')
		scaled_circle_index = _area_index_by_label(selector_map, 'Scaled circle region')
		assert scaled_circle_index is not None
		scaled_circle_result = await tools.click(index=scaled_circle_index, browser_session=browser_session)
		assert scaled_circle_result.error, 'a visually unreachable scaled hotspot must fail explicitly'
		assert 'image-map hotspot' in scaled_circle_result.error.lower()
		assert 'reachable' in scaled_circle_result.error.lower()
		assert await _evaluate(browser_session, 'window.location.hash') == hash_before

	async def test_plain_link_still_uses_regular_click_path(self, browser_session, base_url):
		from browser_use.tools.service import Tools

		tools = Tools()
		await _open_page(tools, browser_session, base_url)

		state = await browser_session.get_browser_state_summary(include_screenshot=False, include_recent_events=False)
		selector_map = state.dom_state.selector_map

		plain_link_index = next(
			(idx for idx, node in selector_map.items() if node.tag_name == 'a' and node.attributes.get('id') == 'plain-link'),
			None,
		)
		assert plain_link_index is not None
		result = await tools.click(index=plain_link_index, browser_session=browser_session)
		assert not result.error, f'plain link click failed: {result.error}'
		await asyncio.sleep(0.3)
		assert await _evaluate(browser_session, 'window.location.hash') == '#plain-anchor'

	async def test_clicking_hotspot_of_hidden_image_fails_explicitly(self, browser_session, base_url):
		from browser_use.tools.service import Tools

		tools = Tools()
		await _open_page(tools, browser_session, base_url)

		state = await browser_session.get_browser_state_summary(include_screenshot=False, include_recent_events=False)
		selector_map = state.dom_state.selector_map
		rect_index = _area_index_by_label(selector_map, 'Rect region')
		assert rect_index is not None

		# Hide the image AFTER indexing and click the stale cached index directly.
		await _evaluate(browser_session, "document.getElementById('main-image').style.display='none'")
		await asyncio.sleep(0.2)

		result = await tools.click(index=rect_index, browser_session=browser_session)
		assert result.error, 'clicking a hotspot of a hidden image must fail'
		assert 'image-map hotspot' in result.error.lower()
		assert 'hidden' in result.error.lower()
		# Must not navigate as a side effect.
		assert await _evaluate(browser_session, 'window.location.hash') == ''
