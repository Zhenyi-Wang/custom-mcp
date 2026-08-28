import pytest

from src.tools.ssrf import SSRFError, assert_url_allowed, is_private_ip, resolve_and_check


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255",
        "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
        "224.0.0.1", "240.0.0.1", "::1", "fe80::1", "fc00::1",
        "::ffff:10.0.0.1", "::ffff:169.254.169.254", "not-an-ip",
    ],
)
def test_private_ips_blocked(ip):
    assert is_private_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_ips_allowed(ip):
    assert is_private_ip(ip) is False


def test_non_http_scheme_rejected():
    with pytest.raises(SSRFError, match="协议"):
        assert_url_allowed("file:///etc/passwd")


def test_unspecified_ipv6_rejected():
    """:: 在 Linux 上与 0.0.0.0 同义,直达本机,必须拦截。"""
    with pytest.raises(SSRFError, match="私有"):
        assert_url_allowed("http://[::]/")


def test_nat64_synthesized_rejected():
    with pytest.raises(SSRFError, match="私有"):
        assert_url_allowed("http://[64:ff9b::7f00:1]/")


def test_literal_private_ip_url_rejected():
    with pytest.raises(SSRFError, match="私有"):
        assert_url_allowed("http://169.254.169.254/latest/meta-data/")


def test_public_url_returns_hostname():
    assert assert_url_allowed("https://example.com/path") == "example.com"


def test_resolve_all_private_blocked():
    def fake_resolver(host, port):
        return [(2, 1, 6, "", ("10.0.0.5", 0)), (2, 1, 6, "", ("192.168.0.9", 0))]

    with pytest.raises(SSRFError, match="私有"):
        resolve_and_check("evil.com", resolver=fake_resolver)


def test_resolve_mixed_addresses_blocked():
    """部分地址私网也拒绝(防 DNS rebinding 挑公网地址绕过)。"""

    def fake_resolver(host, port):
        return [(2, 1, 6, "", ("8.8.8.8", 0)), (2, 1, 6, "", ("10.0.0.5", 0))]

    with pytest.raises(SSRFError):
        resolve_and_check("evil.com", resolver=fake_resolver)


def test_resolve_public_passes():
    def fake_resolver(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    assert resolve_and_check("example.com", resolver=fake_resolver) == ["93.184.216.34"]
