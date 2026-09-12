from pathlib import Path

from deploy.config import parse_config
from deploy.paths import MANAGED_HEADER, Paths
from deploy.render import render_nginx_snippet

PATHS = Paths.under(Path("/srv/test"))


def render(toml: str, port: int | None = 8201) -> str:
    return render_nginx_snippet(parse_config(toml, repo_name="x"), port, PATHS)


def test_service_without_strip_has_no_trailing_slash_on_proxy_pass():
    out = render(
        '[service]\nstart = "run"\n'
        '[nginx]\npath = "/pokemon/"\nstrip_prefix = false\n',
        port=8151,
    )
    assert "proxy_pass http://127.0.0.1:8151;" in out
    assert "proxy_pass http://127.0.0.1:8151/;" not in out


def test_service_with_strip_has_trailing_slash_on_proxy_pass():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/crochet/"\n', port=8152)
    assert "proxy_pass http://127.0.0.1:8152/;" in out


def test_trailing_slash_path_emits_the_redirect():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/pokemon/"\n')
    assert "location = /pokemon { return 301 /pokemon/; }" in out


def test_non_trailing_slash_path_emits_no_redirect():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/blog"\n')
    assert "return 301" not in out
    assert "location /blog {" in out


def test_forwarded_headers_are_always_present():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/blog"\n')
    assert "proxy_set_header Host $host;" in out
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in out
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in out
    assert "proxy_set_header X-Forwarded-Prefix /blog;" in out


def test_client_max_body_size_is_emitted_only_when_set():
    with_size = render(
        '[service]\nstart = "run"\n'
        '[nginx]\npath = "/blog"\nclient_max_body_size = "10m"\n'
    )
    without = render('[service]\nstart = "run"\n[nginx]\npath = "/blog"\n')
    assert "client_max_body_size 10m;" in with_size
    assert "client_max_body_size" not in without


def test_static_app_uses_alias_and_try_files():
    out = render(
        '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "static"\n'
        '[nginx]\npath = "/boggle/"\n',
        port=None,
    )
    assert "alias /srv/test/var/www/deploy/x/;" in out
    assert "try_files $uri $uri/ =404;" in out
    assert "proxy_pass" not in out
    assert "location = /boggle { return 301 /boggle/; }" in out


def test_every_snippet_starts_with_the_managed_header():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/blog"\n')
    assert out.startswith(MANAGED_HEADER + "\n")


def test_snippet_ends_with_exactly_one_newline():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/blog"\n')
    assert out.endswith("}\n")
    assert not out.endswith("\n\n")
