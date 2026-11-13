import json
from pathlib import Path
from kadr_farm.publish_package import create_publish_package

def test_publish_package_lists_real_media_and_unverified_claims(tmp_path):
    script=tmp_path/'script.json'; storyboard=tmp_path/'storyboard.json'
    script.write_text(json.dumps({"title":"Тест", "description_summary":"Описание", "hashtags":["#ИИ"]}),encoding='utf-8')
    storyboard.write_text(json.dumps({"scenes":[{
        "id":"s_001", "claims":[{"text":"Утверждение для ручной проверки", "source_urls":["https://example.org"], "status":"verify_before_publish", "source_availability":["not_checked"], "verified_by_human":False}],
        "media_refs":{"visuals":[{"path":"C:/tmp/clip.mp4", "media_type":"video", "source":"Pixabay", "source_url":"https://pixabay.com/videos/example", "creator":"creator", "license_name":"Pixabay Content License", "rights_status":"review_required", "sha256":"abc"}]}
    }]}),encoding='utf-8')
    result=create_publish_package(script,storyboard,tmp_path/'publish')
    assert result['auto_publication'] is False
    assert len(result['claims_requiring_review'])==1
    assert len(result['external_media_attribution'])==1
    text=(tmp_path/'publish'/'publish.txt').read_text(encoding='utf-8')
    assert 'Pixabay — creator' in text
    assert 'ПЕРЕД ПУБЛИКАЦИЕЙ ПРОВЕРИТЬ ФАКТЫ' in text
