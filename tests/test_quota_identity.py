from __future__ import annotations

from mira_api.quota.identity import (
    hash_ip_prefix,
    new_token,
    resolve_subject_key,
    sign_token,
    verify_signed_token,
)

SECRET = "test-secret-no-es-el-real"


def test_firma_y_verifica_un_token() -> None:
    token = new_token()
    signed = sign_token(token, secret=SECRET)
    assert verify_signed_token(signed, secret=SECRET) == token


def test_rechaza_token_alterado() -> None:
    token = new_token()
    signed = sign_token(token, secret=SECRET)
    tampered = signed[:-1] + ("0" if signed[-1] != "0" else "1")
    assert verify_signed_token(tampered, secret=SECRET) is None


def test_rechaza_token_firmado_con_otro_secreto() -> None:
    token = new_token()
    signed = sign_token(token, secret="otro-secreto")
    assert verify_signed_token(signed, secret=SECRET) is None


def test_rechaza_formato_invalido() -> None:
    assert verify_signed_token("no-tiene-punto", secret=SECRET) is None


def test_ipv4_se_hashea_al_prefijo_32() -> None:
    a = hash_ip_prefix("203.0.113.7", secret=SECRET)
    b = hash_ip_prefix("203.0.113.7", secret=SECRET)
    assert a == b
    assert a is not None
    # Nunca la IP en claro en el resultado.
    assert "203.0.113.7" not in a


def test_ipv6_se_hashea_al_prefijo_64_no_128() -> None:
    # Dos direcciones distintas dentro del mismo /64 deben hashear igual --
    # eso es lo que dice el plan de arquitectura para IPv6.
    a = hash_ip_prefix("2001:db8:abcd:0012:0000:0000:0000:0001", secret=SECRET)
    b = hash_ip_prefix("2001:db8:abcd:0012:ffff:ffff:ffff:ffff", secret=SECRET)
    assert a == b


def test_ip_invalida_devuelve_none() -> None:
    assert hash_ip_prefix("no-es-una-ip", secret=SECRET) is None


def test_resolve_subject_key_prefiere_cookie_valida() -> None:
    token = new_token()
    cookie = sign_token(token, secret=SECRET)
    key, kind = resolve_subject_key(cookie_value=cookie, client_ip="203.0.113.7", secret=SECRET)
    assert kind == "token"
    assert key == cookie


def test_resolve_subject_key_cae_a_ip_sin_cookie() -> None:
    key, kind = resolve_subject_key(cookie_value=None, client_ip="203.0.113.7", secret=SECRET)
    assert kind == "net"
    assert key == hash_ip_prefix("203.0.113.7", secret=SECRET)


def test_resolve_subject_key_cae_a_ip_con_cookie_invalida() -> None:
    key, kind = resolve_subject_key(
        cookie_value="cookie-basura", client_ip="203.0.113.7", secret=SECRET
    )
    assert kind == "net"
    assert key == hash_ip_prefix("203.0.113.7", secret=SECRET)


def test_resolve_subject_key_sin_cookie_ni_ip() -> None:
    key, kind = resolve_subject_key(cookie_value=None, client_ip=None, secret=SECRET)
    assert key == "anonymous"
    assert kind == "net"
