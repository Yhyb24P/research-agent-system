"""Release tag policy including the explicitly untagged Preview lane."""

import pytest

from scripts.version_policy_check import tag_for_version, version_for_tag


def test_developer_preview_version_has_no_release_tag() -> None:
    assert tag_for_version("1.0.0rc83.dev0") is None


def test_rc_and_final_versions_keep_exact_tag_mapping() -> None:
    assert tag_for_version("1.2.3rc4") == "v1.2.3-rc.4"
    assert tag_for_version("1.2.3") == "v1.2.3"
    assert version_for_tag("v1.2.3-rc.4") == "1.2.3rc4"
    assert version_for_tag("v1.2.3") == "1.2.3"


@pytest.mark.parametrize("version", ("1.0", "1.0.0.dev0", "1.0.0rc0.dev0"))
def test_unknown_version_shapes_fail_closed(version: str) -> None:
    with pytest.raises(ValueError):
        tag_for_version(version)
