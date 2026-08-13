"""Reverse-proxy aware client address resolution.

Rate limiting is only meaningful when the API can tell one browser client from
another. Behind the bundled Nginx container every request arrives from the same
container address, so ``X-Forwarded-For`` must be consulted -- but only when the
direct peer is a configured trusted proxy, otherwise any client could forge its
own identity.
"""

from __future__ import annotations

from collections.abc import Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

TrustedNetworks = tuple[IPv4Network | IPv6Network, ...]


def parse_trusted_networks(value: str) -> TrustedNetworks:
    """Parse a comma-separated address/CIDR allowlist of reverse-proxy peers."""

    networks: list[IPv4Network | IPv6Network] = []
    for entry in value.split(","):
        candidate = entry.strip()
        if candidate:
            networks.append(ip_network(candidate, strict=False))
    return tuple(networks)


def resolve_client_ip(
    *,
    peer_host: str | None,
    forwarded_for: Sequence[str],
    trusted_networks: TrustedNetworks,
) -> str | None:
    """Return the originating client address, or ``None`` when it is unknown.

    The forwarded chain is walked from the right so that a client-supplied
    prefix can never displace the address appended by the trusted proxy. An
    unparsable hop makes everything to its left untrustworthy, so resolution
    fails closed rather than attributing the request to a forged address.
    """

    if peer_host is None:
        return None
    if not trusted_networks or not _is_trusted_host(peer_host, trusted_networks):
        return peer_host

    entries = [item.strip() for value in forwarded_for for item in value.split(",")]
    for candidate in reversed(entries):
        if not candidate:
            continue
        try:
            parsed = ip_address(candidate)
        except ValueError:
            return None
        if _is_trusted_address(parsed, trusted_networks):
            continue
        return candidate
    return peer_host


def _is_trusted_host(host: str, networks: TrustedNetworks) -> bool:
    try:
        parsed = ip_address(host)
    except ValueError:
        return False
    return _is_trusted_address(parsed, networks)


def _is_trusted_address(
    address: IPv4Address | IPv6Address, networks: TrustedNetworks
) -> bool:
    return any(address in network for network in networks)
