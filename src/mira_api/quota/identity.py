from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets

#: Nombre de la cookie anonima de cuota. HttpOnly/Secure/SameSite=Lax se
#: configuran donde se establece la cookie (capa HTTP), no aqui -- este modulo
#: es puro computo, sin FastAPI ni I/O, para poder probarlo sin un request real.
COOKIE_NAME = "mira_subject"
TOKEN_BYTES = 32


def new_token() -> str:
    """Token anonimo aleatorio para la cookie. No identifica a la persona --
    solo distingue "misma sesion de navegador" de "otra"."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def sign_token(token: str, *, secret: str) -> str:
    """HMAC del token, para que un cliente no pueda fabricar uno propio y
    resetear su cuota escribiendo la cookie a mano."""
    digest = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    return f"{token}.{digest}"


def verify_signed_token(signed: str, *, secret: str) -> str | None:
    """Devuelve el token si la firma es valida, None si la cookie fue alterada
    o no tiene el formato esperado."""
    if "." not in signed:
        return None
    token, _, digest = signed.rpartition(".")
    expected = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, expected):
        return None
    return token


def hash_ip_prefix(ip: str, *, secret: str) -> str | None:
    """HMAC del prefijo de red (/32 IPv4, /64 IPv6) -- nunca se persiste la IP
    en claro, ni siquiera el prefijo: solo su hash. Sirve de respaldo cuando no
    hay cookie (primera visita, o el cliente la borro)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address):
        network = ipaddress.ip_network(f"{addr}/64", strict=False)
    else:
        network = ipaddress.ip_network(f"{addr}/32", strict=False)
    address_bytes = str(network.network_address).encode()
    return hmac.new(secret.encode(), address_bytes, hashlib.sha256).hexdigest()


def resolve_subject_key(
    *, cookie_value: str | None, client_ip: str | None, secret: str
) -> tuple[str, str]:
    """Combina cookie y IP: el token de cookie manda si es valido (subject_kind
    'token'); si no hay cookie o no verifica, cae al hash de IP (subject_kind
    'net'). Devuelve (subject_key, subject_kind) -- nunca la IP en claro.

    Este modulo no depende de si quota.subject esta activo: siempre puede
    calcularse, y quien llama decide si lo usa para limitar algo.
    """
    if cookie_value:
        token = verify_signed_token(cookie_value, secret=secret)
        if token is not None:
            return sign_token(token, secret=secret), "token"

    if client_ip:
        ip_hash = hash_ip_prefix(client_ip, secret=secret)
        if ip_hash is not None:
            return ip_hash, "net"

    return "anonymous", "net"
