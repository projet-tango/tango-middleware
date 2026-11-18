"""Printable tags: the SVG must decode to the right id; print page and ZIP export."""
import io
import re
import zipfile

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from tango_mw.app import App
from tango_mw.config import AppConfig
from tango_mw.tagsheet import tag_svg
from tango_mw.vision.detector import TagDetector
from tango_mw.web.server import create_web_app


def rasterize(svg: str, px_per_mm: float = 4.0) -> np.ndarray:
    w, h = (float(v) for v in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups())
    img = np.full((int(h * px_per_mm) + 40, int(w * px_per_mm) + 40), 255, np.uint8)
    for x, y, rw, rh in re.findall(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"/>', svg):
        x0, y0 = int(float(x) * px_per_mm) + 20, int(float(y) * px_per_mm) + 20
        img[y0:y0 + int(float(rh) * px_per_mm), x0:x0 + int(float(rw) * px_per_mm)] = 0
    return img


@pytest.mark.parametrize("tag_id", [0, 3, 10, 42])
def test_svg_tag_decodes_to_its_id(tag_id):
    svg = tag_svg("tag36h11", tag_id, 100, caption=f"#{tag_id}", front=True)
    assert 'width="' in svg and "mm" in svg
    dets = TagDetector(AppConfig().detector).detect(cv2.cvtColor(rasterize(svg), cv2.COLOR_GRAY2BGR))
    assert [d.id for d in dets] == [tag_id]


@pytest.fixture
def client(tmp_path):
    cfg = AppConfig()
    cfg.ros.bus = "loopback"
    cfg.plugins.dirs = [str(tmp_path / "c")]
    app = App(cfg, tmp_path / "config.yaml", base_dir=tmp_path)
    return TestClient(create_web_app(app))


def test_print_page_and_archive(client):
    r = client.get("/print/tags", params={"which": "all", "robot_size_mm": 60})
    assert r.status_code == 200 and r.text.count("<svg") == 6 + 1  # 4 boundary + 2 robots + ruler
    assert "FRONT" in r.text and "100 mm" in r.text
    r = client.get("/api/tags/archive.zip", params={"which": "world"})
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len([n for n in names if n.endswith(".svg")]) == 4 and len([n for n in names if n.endswith(".png")]) == 4
    assert client.get("/api/tags/7.svg", params={"size_mm": 80}).headers["content-type"].startswith("image/svg")
    assert client.get("/print/tags", params={"which": "custom", "ids": "5,6"}).text.count("<svg") == 3
    assert client.get("/api/tags/list").json()["items"][0]["size_mm"] == 100
