from kadr_farm.scene_validator import validate_timing

def test_timing_warnings_are_not_hard_limits():
    assert validate_timing({"id":"s_001","duration_sec":2.5})
    assert validate_timing({"id":"s_002","duration_sec":28.0})
    assert validate_timing({"id":"s_003","duration_sec":18.0}) == []
