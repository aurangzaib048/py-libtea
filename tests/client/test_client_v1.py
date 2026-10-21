from unittest.mock import patch

import pytest
import responses
from responses import matchers

from libtea._validation import _validate, _validate_list
from libtea.client import TeaClient
from libtea.exceptions import TeaServerError, TeaValidationError
from tests.spec_data import (
    ARTIFACT,
    COMPONENT,
    COMPONENT_RELEASE,
    LEGACY_BASE_URL,
    MD5_HEX,
    PRODUCT,
    PRODUCT_RELEASE,
    V1_ARTIFACT,
    V1_BASE_URL,
    V1_COLLECTION,
    V1_PRODUCT_RELEASE,
    V1_RELEASE,
    product,
)


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
    with pytest.raises(TeaValidationError, match="TEA 1.0.0"):
        TeaClient(V1_BASE_URL).get_component_release_collection_latest(COMPONENT_RELEASE)


@responses.activate
def test_v1_artifact_with_md5_rejected():
    art = {
        **V1_ARTIFACT,
        "formats": [{**V1_ARTIFACT["formats"][0], "checksums": [{"algType": "MD5", "algValue": MD5_HEX}]}],
    }
    responses.get(f"{V1_BASE_URL}/artifact/{ARTIFACT}/latest", json=art)
    with pytest.raises(TeaValidationError, match="MD5"):
        TeaClient(V1_BASE_URL).get_artifact(ARTIFACT)


# --- Token pagination ---


@responses.activate
def test_first_page_omits_token(v1):
    responses.get(
        f"{V1_BASE_URL}/products",
        match=[matchers.query_param_matcher({"pageSize": "100"})],
        json={"hasNext": False, "results": []},
    )
    assert v1.list_products().has_next is False
    assert len(responses.calls) == 1


@responses.activate
def test_page_token_sent(v1):
    responses.get(
        f"{V1_BASE_URL}/products",
        match=[matchers.query_param_matcher({"pageSize": "100", "pageToken": "tok-1"})],
        json={"hasNext": False, "results": []},
    )
    v1.list_products(page_token="tok-1")
    assert len(responses.calls) == 1


def test_page_size_capped_at_100_on_v1(v1):
    with pytest.raises(TeaValidationError, match="between 1 and 100"):
        v1.list_products(page_size=101)


@responses.activate
def test_iter_follows_tokens(v1):
    responses.get(
        f"{V1_BASE_URL}/productReleases",
        match=[matchers.query_param_matcher({"idType": "PURL", "idValue": "pkg:x", "pageSize": "100"})],
        json={"hasNext": True, "nextPageToken": "t2", "results": [V1_PRODUCT_RELEASE]},
    )
    responses.get(
        f"{V1_BASE_URL}/productReleases",
        match=[
            matchers.query_param_matcher({"idType": "PURL", "idValue": "pkg:x", "pageSize": "100", "pageToken": "t2"})
        ],
        json={"hasNext": False, "results": [V1_PRODUCT_RELEASE]},
    )
    assert len(list(v1.iter_product_releases("PURL", "pkg:x"))) == 2


@responses.activate
def test_iter_raises_on_repeated_token(v1):  # Review Focus 1
    responses.get(
        f"{V1_BASE_URL}/products",
        json={"hasNext": True, "nextPageToken": "same", "results": [product(PRODUCT, "P")]},
    )
    with pytest.raises(TeaServerError, match="repeated"):
        list(v1.iter_products("PURL", "pkg:x"))


@responses.activate
def test_iter_raises_on_token_cycle(v1):
    url = f"{V1_BASE_URL}/products"
    for sent, nxt in [(None, "A"), ("A", "B"), ("B", "A")]:
        query = {"idType": "PURL", "idValue": "pkg:x", "pageSize": "100", **({"pageToken": sent} if sent else {})}
        responses.get(
            url,
            match=[matchers.query_param_matcher(query)],
            json={"hasNext": True, "nextPageToken": nxt, "results": [product(PRODUCT, "P")]},
        )
    with pytest.raises(TeaServerError, match="repeated"):
        list(v1.iter_products("PURL", "pkg:x"))
    assert len(responses.calls) == 3


@responses.activate
def test_iter_stops_at_page_cap(v1, monkeypatch):
    monkeypatch.setattr("libtea.client._MAX_PAGES", 2)
    url = f"{V1_BASE_URL}/products"
    for sent, nxt in [(None, "t1"), ("t1", "t2")]:
        query = {"idType": "PURL", "idValue": "pkg:x", "pageSize": "100", **({"pageToken": sent} if sent else {})}
        responses.get(
            url,
            match=[matchers.query_param_matcher(query)],
            json={"hasNext": True, "nextPageToken": nxt, "results": [product(PRODUCT, "P")]},
        )
    with pytest.raises(TeaServerError, match="Pagination exceeded 2 pages"):
        list(v1.iter_products("PURL", "pkg:x"))
    assert len(responses.calls) == 2


@responses.activate
def test_iter_stops_on_empty_page(v1):
    responses.get(f"{V1_BASE_URL}/products", json={"hasNext": True, "nextPageToken": "t", "results": []})
    assert list(v1.iter_products("PURL", "pkg:x")) == []
    assert len(responses.calls) == 1


@responses.activate
def test_component_releases_collects_pages_on_v1(v1):
    url = f"{V1_BASE_URL}/component/{COMPONENT}/releases"
    responses.get(
        url,
        match=[matchers.query_param_matcher({"pageSize": "100"})],
        json={"hasNext": True, "nextPageToken": "n", "results": [V1_RELEASE]},
    )
    responses.get(
        url,
        match=[matchers.query_param_matcher({"pageSize": "100", "pageToken": "n"})],
        json={"hasNext": False, "results": [V1_RELEASE]},
    )
    assert len(v1.get_component_releases(COMPONENT)) == 2


@responses.activate
@pytest.mark.parametrize(
    ("method", "path", "uuid"),
    [
        ("get_product_release_collections", "productRelease", PRODUCT_RELEASE),
        ("get_component_release_collections", "componentRelease", COMPONENT_RELEASE),
    ],
)
def test_collections_collects_pages_on_v1(v1, method, path, uuid):
    url = f"{V1_BASE_URL}/{path}/{uuid}/collections"
    responses.get(
        url,
        match=[matchers.query_param_matcher({"pageSize": "100"})],
        json={"hasNext": True, "nextPageToken": "n", "results": [V1_COLLECTION]},
    )
    responses.get(
        url,
        match=[matchers.query_param_matcher({"pageSize": "100", "pageToken": "n"})],
        json={"hasNext": False, "results": [V1_COLLECTION]},
    )
    assert len(getattr(v1, method)(uuid)) == 2


@responses.activate
def test_legacy_token_maps_to_offset(legacy):
    responses.get(
        f"{LEGACY_BASE_URL}/products",
        match=[matchers.query_param_matcher({"pageOffset": "200", "pageSize": "100"})],
        json={
            "timestamp": "2024-03-20T15:30:00Z",
            "pageStartIndex": 200,
            "pageSize": 100,
            "totalResults": 200,
            "results": [],
        },
    )
    legacy.list_products(page_token="200")
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("token", ["abc", "-1", "²", ""])
def test_legacy_non_numeric_token_rejected(legacy, token):  # Review Focus 2
    with pytest.raises(TeaValidationError, match="integer offset"):
        legacy.list_products(page_token=token)
    assert len(responses.calls) == 0


def test_legacy_page_size_allows_10000(legacy):
    with pytest.raises(TeaValidationError, match="between 1 and 10000"):
        legacy.list_products(page_size=10001)


@responses.activate
def test_legacy_page_size_10000_accepted(legacy):
    responses.get(
        f"{LEGACY_BASE_URL}/products",
        match=[matchers.query_param_matcher({"pageOffset": "0", "pageSize": "10000"})],
        json={"timestamp": "2024-03-20T15:30:00Z", "pageStartIndex": 0, "pageSize": 10000, "totalResults": 0},
    )
    legacy.list_products(page_size=10000)
    assert len(responses.calls) == 1


@responses.activate
def test_get_product_release_v1(v1):
    responses.get(
        f"{V1_BASE_URL}/productRelease/{PRODUCT_RELEASE}",
        json={**V1_PRODUCT_RELEASE, "latestCollection": {**V1_COLLECTION, "belongsTo": "PRODUCT_RELEASE"}},
    )
    assert v1.get_product_release(PRODUCT_RELEASE).latest_collection.belongs_to == "PRODUCT_RELEASE"


@responses.activate
def test_get_product_release_v1_without_collection_rejected(v1):
    responses.get(f"{V1_BASE_URL}/productRelease/{PRODUCT_RELEASE}", json=V1_PRODUCT_RELEASE)
    with pytest.raises(TeaValidationError, match="latest_collection"):
        v1.get_product_release(PRODUCT_RELEASE)


@responses.activate
def test_get_component_release_v1_flattened(v1):
    responses.get(
        f"{V1_BASE_URL}/componentRelease/{COMPONENT_RELEASE}",
        json={**V1_RELEASE, "latestCollection": V1_COLLECTION},
    )
    result = v1.get_component_release(COMPONENT_RELEASE)
    assert (result.release.component, result.latest_collection.version) == (COMPONENT, 2)


@responses.activate
def test_get_component_release_legacy_nested(legacy):
    responses.get(
        f"{LEGACY_BASE_URL}/componentRelease/{COMPONENT_RELEASE}",
        json={"release": V1_RELEASE, "latestCollection": V1_COLLECTION},
    )
    assert legacy.get_component_release(COMPONENT_RELEASE).release.uuid == COMPONENT_RELEASE
