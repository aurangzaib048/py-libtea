from unittest.mock import patch

import pytest
import responses

from libtea._validation import _validate, _validate_list
from libtea.client import TeaClient
from libtea.exceptions import TeaValidationError
from tests.spec_data import COMPONENT_RELEASE, LEGACY_BASE_URL, PRODUCT, V1_BASE_URL, product


class TestSpecVersion:
    def test_inferred_from_base_url(self):
        c = TeaClient(LEGACY_BASE_URL)
        assert (c.spec_version, c.is_legacy) == ("0.4.0", True)

    def test_build_metadata_kept(self):
        assert TeaClient("https://api.example.com/v1.0.0+build.5").spec_version == "1.0.0+build.5"

    def test_defaults_to_1_0_0_without_semver_suffix(self):
        c = TeaClient("https://api.example.com/tea/v1")
        assert (c.spec_version, c.is_legacy) == ("1.0.0", False)

    def test_explicit_overrides_url(self):
        assert TeaClient(V1_BASE_URL, spec_version="0.4.0").is_legacy is True

    @pytest.mark.parametrize("v", ["2.0.0", "not-semver"])
    def test_rejects_unsupported(self, v):
        with pytest.raises(ValueError, match="Unsupported TEA spec version"):
            TeaClient("https://api.example.com", spec_version=v)


CASES = [("legacy", LEGACY_BASE_URL, True), ("v1", V1_BASE_URL, False)]


class TestLegacyFlagReachesValidation:
    @responses.activate
    @pytest.mark.parametrize("fixture, base_url, expected", CASES)
    def test_validate(self, request, fixture, base_url, expected):
        client = request.getfixturevalue(fixture)
        responses.get(f"{base_url}/product/{PRODUCT}", json=product(PRODUCT, "Widget"))
        with patch("libtea.client._validate", wraps=_validate) as spy:
            client.get_product(PRODUCT)
        assert spy.call_args.kwargs == {"legacy": expected}

    @responses.activate
    @pytest.mark.parametrize("fixture, base_url, expected", CASES)
    def test_validate_list(self, request, fixture, base_url, expected):
        client = request.getfixturevalue(fixture)
        responses.get(f"{base_url}/discovery", json=[])
        with patch("libtea.client._validate_list", wraps=_validate_list) as spy:
            client.discover("urn:tei:purl:example.com:pkg:pypi/lib@1.0")
        assert spy.call_args.kwargs == {"legacy": expected}


@responses.activate
def test_v1_client_rejects_incomplete_collection():
    responses.get(
        f"{V1_BASE_URL}/componentRelease/{COMPONENT_RELEASE}/collection/latest",
        json={"uuid": COMPONENT_RELEASE, "version": 1},
    )
    with pytest.raises(TeaValidationError, match="Collection"):
        TeaClient(V1_BASE_URL).get_component_release_collection_latest(COMPONENT_RELEASE)
