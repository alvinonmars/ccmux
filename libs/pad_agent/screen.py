"""Screen reading via UIAutomator — parse the accessibility tree.

Dumps the UI hierarchy XML from the device and extracts interactive
elements with their bounds, text, and resource IDs.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .adb import ADB

DUMP_PATH = "/sdcard/window_dump.xml"


@dataclass
class UIElement:
    """A single UI element extracted from the accessibility tree."""

    index: int
    text: str
    resource_id: str
    class_name: str
    content_desc: str
    clickable: bool
    bounds: tuple[int, int, int, int]  # (x1, y1, x2, y2)
    children: list[UIElement] = field(default_factory=list)

    @property
    def center(self) -> tuple[int, int]:
        """Center point of the element — used for tap targets."""
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def label(self) -> str:
        """Best human-readable label for this element."""
        return self.text or self.content_desc or self.resource_id or self.class_name


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_bounds(bounds_str: str) -> tuple[int, int, int, int]:
    m = _BOUNDS_RE.match(bounds_str)
    if not m:
        return (0, 0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def _parse_element(node: ET.Element) -> UIElement:
    return UIElement(
        index=int(node.attrib.get("index", 0)),
        text=node.attrib.get("text", ""),
        resource_id=node.attrib.get("resource-id", ""),
        class_name=node.attrib.get("class", ""),
        content_desc=node.attrib.get("content-desc", ""),
        clickable=node.attrib.get("clickable", "false") == "true",
        bounds=_parse_bounds(node.attrib.get("bounds", "")),
        children=[_parse_element(child) for child in node],
    )


class Screen:
    """Read and parse the device screen via UIAutomator."""

    def __init__(self, adb: ADB):
        self._adb = adb

    def dump_hierarchy(self) -> UIElement:
        """Dump the full UI hierarchy and return the root element."""
        self._adb.shell(f"uiautomator dump {DUMP_PATH}", timeout=15)
        xml_content = self._adb.shell(f"cat {DUMP_PATH}", timeout=10)
        root = ET.fromstring(xml_content)
        return _parse_element(root)

    def find_elements(
        self,
        *,
        text: str = "",
        resource_id: str = "",
        class_name: str = "",
        clickable: bool | None = None,
    ) -> list[UIElement]:
        """Dump the screen and find elements matching the given filters."""
        root = self.dump_hierarchy()
        return _collect_matching(root, text, resource_id, class_name, clickable)

    def get_clickable_elements(self) -> list[UIElement]:
        """Get all clickable elements on the current screen."""
        return self.find_elements(clickable=True)

    def get_text_content(self) -> list[str]:
        """Extract all visible text from the screen."""
        root = self.dump_hierarchy()
        texts: list[str] = []
        _collect_texts(root, texts)
        return [t for t in texts if t]


def _collect_matching(
    element: UIElement,
    text: str,
    resource_id: str,
    class_name: str,
    clickable: bool | None,
) -> list[UIElement]:
    results: list[UIElement] = []
    match = True
    if text and text.lower() not in element.text.lower():
        match = False
    if resource_id and resource_id not in element.resource_id:
        match = False
    if class_name and class_name not in element.class_name:
        match = False
    if clickable is not None and element.clickable != clickable:
        match = False
    if match:
        results.append(element)
    for child in element.children:
        results.extend(
            _collect_matching(child, text, resource_id, class_name, clickable)
        )
    return results


def _collect_texts(element: UIElement, out: list[str]) -> None:
    if element.text:
        out.append(element.text)
    if element.content_desc:
        out.append(element.content_desc)
    for child in element.children:
        _collect_texts(child, out)
