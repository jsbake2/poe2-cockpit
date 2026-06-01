from service.focus import matches_required


def test_none_required_always_true():
    assert matches_required("anything", None)
    assert matches_required("", None)


def test_empty_required_always_true():
    assert matches_required("foo", "")


def test_substring_match_case_insensitive():
    assert matches_required("Path of Exile 2", "path of exile")
    assert matches_required("Path of Exile 2", "Path of Exile")
    assert matches_required("PATH OF EXILE", "path of exile")


def test_no_match():
    assert not matches_required("Firefox", "Path of Exile")


def test_empty_title_with_required():
    assert not matches_required("", "Path of Exile")
