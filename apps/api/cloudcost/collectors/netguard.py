"""Validación de URLs y destinos de red que un cliente configura (Qdrant, PostgreSQL): nunca los metadatos de la nube ni direcciones de enlace local."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


class DestinationError(ValueError):
    pass


def check_host(host: str, label: str) -> str:
    host = (host or "").lower().strip("[]")
    if not host:
        raise DestinationError(f"{label}: falta el host")
    if host in BLOCKED_HOSTS:
        raise DestinationError(f"{label}: host no permitido")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        raise DestinationError(f"{label}: dirección no permitida (enlace local, metadatos de la nube)")
    return host


def is_private_or_internal(host: str) -> bool:
    """Loopback, red privada, nombre de servicio de clúster/Docker (sin puntos) o `.svc`: tráfico que no sale a Internet."""
    host = host.lower().strip("[]")
    if host == "localhost" or host.endswith((".svc", ".svc.cluster.local", ".internal", ".local")) or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def validate_service_url(url: str, label: str) -> str:
    """https siempre; http solo dentro de la red interna. Sin credenciales, parámetros ni fragmento en la URL. Devuelve la URL sin barra final."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DestinationError(f"{label}: debe ser http(s)://host[:puerto]")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DestinationError(f"{label}: no puede llevar credenciales, parámetros ni fragmento")
    host = check_host(parsed.hostname, label)
    if parsed.scheme == "http" and not is_private_or_internal(host):
        raise DestinationError(f"{label}: http solo se admite en una red interna (localhost, IP privada, nombre de servicio); usa https")
    return url.rstrip("/")
