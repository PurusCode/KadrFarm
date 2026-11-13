from kadr_farm.scene_validator import enrich_claims

def test_claims_are_never_auto_verified():
    claims = enrich_claims([
        {"text":"A factual claim to review", "source_urls":["https://example.org"]},
        {"text":"An unsourced statement needing review", "source_urls":[]},
    ])
    assert claims[0]["status"] == "verify_before_publish"
    assert claims[0]["source_availability"] == ["not_checked"]
    assert claims[1]["status"] == "no_source"
    assert all(row["verified_by_human"] is False for row in claims)
