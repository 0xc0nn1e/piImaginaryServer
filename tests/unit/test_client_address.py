from __future__ import annotations

import pytest

from audio_server.core.client_address import parse_trusted_networks, resolve_client_ip

PROXY = "172.20.0.5"
TRUSTED = parse_trusted_networks("172.20.0.0/16")
CLIENT = "203.0.113.7"


def test_no_configured_proxy_uses_the_direct_peer() -> None:
    resolved = resolve_client_ip(
        peer_host=PROXY,
        forwarded_for=[CLIENT],
        trusted_networks=(),
    )

    assert resolved == PROXY


def test_untrusted_peer_cannot_claim_a_forwarded_address() -> None:
    resolved = resolve_client_ip(
        peer_host="198.51.100.9",
        forwarded_for=[CLIENT],
        trusted_networks=TRUSTED,
    )

    assert resolved == "198.51.100.9"


def test_trusted_proxy_reveals_the_original_client() -> None:
    resolved = resolve_client_ip(
        peer_host=PROXY,
        forwarded_for=[CLIENT],
        trusted_networks=TRUSTED,
    )

    assert resolved == CLIENT


def test_client_supplied_prefix_cannot_displace_the_proxy_appended_address() -> None:
    # Nginx appends the real peer, so the forged left-hand entry is never reached.
    resolved = resolve_client_ip(
        peer_host=PROXY,
        forwarded_for=[f"1.2.3.4, {CLIENT}"],
        trusted_networks=TRUSTED,
    )

    assert resolved == CLIENT


def test_repeated_headers_and_further_trusted_hops_are_skipped() -> None:
    resolved = resolve_client_ip(
        peer_host=PROXY,
        forwarded_for=[CLIENT, "172.20.0.9, 172.20.0.4"],
        trusted_networks=TRUSTED,
    )

    assert resolved == CLIENT


def test_unparsable_hop_fails_closed() -> None:
    resolved = resolve_client_ip(
        peer_host=PROXY,
        forwarded_for=["not-an-address"],
        trusted_networks=TRUSTED,
    )

    assert resolved is None


def test_trusted_proxy_without_a_forwarded_header_falls_back_to_the_peer() -> None:
    resolved = resolve_client_ip(
        peer_host=PROXY,
        forwarded_for=[],
        trusted_networks=TRUSTED,
    )

    assert resolved == PROXY


def test_missing_peer_is_unknown() -> None:
    assert (
        resolve_client_ip(peer_host=None, forwarded_for=[CLIENT], trusted_networks=TRUSTED)
        is None
    )


def test_parse_trusted_networks_accepts_addresses_and_ranges() -> None:
    networks = parse_trusted_networks(" 10.0.0.1 , 172.20.0.0/16 ,")

    assert [str(network) for network in networks] == ["10.0.0.1/32", "172.20.0.0/16"]


def test_parse_trusted_networks_rejects_invalid_entries() -> None:
    with pytest.raises(ValueError):
        parse_trusted_networks("172.20.0.0/16, not-a-network")
