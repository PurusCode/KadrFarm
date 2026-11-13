from pathlib import Path
from PIL import Image
from kadr_farm.diagram_renderer import render_scene_visual

def test_chart_renderer_uses_only_supplied_values_and_supports_negative_values(tmp_path):
    path=render_scene_visual({
        "id":"s_001", "meaning":"Динамика показателя", "visual_type":"chart",
        "visual_description":"График изменения реального показателя",
        "chart_data":{"title":"Изменение", "labels":["A","B","C"],
          "values":[-2.0, 0.0, 4.5], "unit":"%", "source_url":"https://example.org",
          "methodology":"Исходные значения из опубликованной таблицы"},
        "on_screen_text":["Исходные данные"]
    }, tmp_path/"chart.png")
    assert path.is_file()
    with Image.open(path) as image:
        assert image.size == (1920,1080)
