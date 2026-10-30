

def test_timeout_ms_must_be_a_positive_int():
    import pytest
    from nxthooks.manifest import ManifestError, _validate

    base = {"name": "t", "events": ["before_tool"], "source": "x.sh"}
    assert _validate({**base, "timeout_ms": 500}, "m")
    for bad in ("5", 0, -1, True):
        with pytest.raises(ManifestError):
            _validate({**base, "timeout_ms": bad}, "m")
