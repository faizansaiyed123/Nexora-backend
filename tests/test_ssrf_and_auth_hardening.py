"""Tests for SSRF hardening and authentication safety."""

import hashlib
import socket

import httpx
import pytest

from backend.core import security
from backend.services.safe_fetch import safe_get
from backend.services.url_security import SecurityValidationError, UrlSecurityService

@pytest.mark.parametrize("ip",["127.0.0.1","10.1.2.3","192.168.0.10","169.254.169.254","100.64.0.1","::1","::ffff:10.0.0.1","::ffff:127.0.0.1","fe80::1%eth0"])
def test_internal_addresses_are_blocked(ip):
    assert UrlSecurityService.is_ip_blocked(ip) is True

@pytest.mark.parametrize("ip",["8.8.8.8","1.1.1.1","2606:4700:4700::1111"])
def test_public_addresses_are_allowed(ip):
    assert UrlSecurityService.is_ip_blocked(ip) is False

@pytest.fixture
def fake_dns(monkeypatch):
    table={"public.example":"93.184.216.34","internal.example":"10.0.0.5"}
    def fake_getaddrinfo(host,port,*args,**kwargs):
        ip=table.get(host)
        if ip is None:
            raise socket.gaierror(host)
        return [(socket.AF_INET,socket.SOCK_STREAM,6,"",(ip,port))]
    monkeypatch.setattr(socket,"getaddrinfo",fake_getaddrinfo)

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)

@pytest.mark.asyncio
async def test_safe_get_returns_body(fake_dns):
    async with _client(lambda r:httpx.Response(200,text="hello",headers={"content-type":"text/html"})) as c:
        resp=await safe_get(c,"https://public.example/page")
    assert resp.status_code==200 and resp.text=="hello"

@pytest.mark.asyncio
async def test_redirect_to_internal_host_is_blocked(fake_dns):
    def handler(request):
        if request.url.host=="public.example":
            return httpx.Response(302,headers={"location":"http://internal.example/admin"})
        pytest.fail("internal host must never be requested")
    async with _client(handler) as c:
        with pytest.raises(SecurityValidationError):
            await safe_get(c,"https://public.example/start")

@pytest.mark.asyncio
async def test_redirect_chain_to_public_host_is_followed(fake_dns):
    def handler(request):
        if request.url.path=="/start":
            return httpx.Response(301,headers={"location":"/final"})
        return httpx.Response(200,text="done")
    async with _client(handler) as c:
        resp=await safe_get(c,"https://public.example/start")
    assert resp.text=="done" and resp.url.endswith("/final")

@pytest.mark.asyncio
async def test_redirect_loop_is_rejected(fake_dns):
    async with _client(lambda r:httpx.Response(302,headers={"location":"/again"})) as c:
        with pytest.raises(SecurityValidationError,match="redirect"):
            await safe_get(c,"https://public.example/loop",max_redirects=3)

@pytest.mark.asyncio
async def test_oversized_body_is_rejected(fake_dns):
    async with _client(lambda r:httpx.Response(200,content=b"x"*2048)) as c:
        with pytest.raises(SecurityValidationError,match="size"):
            await safe_get(c,"https://public.example/big",max_bytes=1024)

def test_argon2_roundtrip_and_no_rehash():
    h=security.get_password_hash("correct horse battery staple")
    assert h.startswith("$argon2id$")
    assert security.verify_password("correct horse battery staple",h)
    assert not security.verify_password("wrong",h)
    assert security.password_needs_rehash(h) is False

def test_legacy_pbkdf2_hash_still_verifies_and_is_flagged_for_upgrade():
    salt="abc123"
    key=hashlib.pbkdf2_hmac("sha256",b"old-password",salt.encode(),100_000).hex()
    legacy="pbkdf2_sha256$"+salt+"$"+key
    assert security.verify_password("old-password",legacy)
    assert not security.verify_password("nope",legacy)
    assert security.password_needs_rehash(legacy) is True

def test_dummy_hash_is_a_real_argon2_hash():
    assert security.DUMMY_PASSWORD_HASH.startswith("$argon2id$")
    assert security.verify_password("anything",security.DUMMY_PASSWORD_HASH) is False

def test_garbage_hash_does_not_raise():
    assert security.verify_password("x","not-a-hash") is False

def test_jwt_errors_do_not_leak_parser_details():
    from backend.core.exceptions import AuthenticationException
    with pytest.raises(AuthenticationException) as exc:
        security.decode_and_validate_access_token("not.a.jwt")
    assert exc.value.code=="INVALID_TOKEN"
    assert "Not enough segments" not in str(exc.value)
