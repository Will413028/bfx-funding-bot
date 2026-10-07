"""Tests for db._prepare_engine_kwargs — D2 async URL transform."""
import ssl
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from bfx_funding_bot.core.db import _prepare_engine_kwargs


def _self_signed_ca_pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bfx-test-ca")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


class TestAsyncUrlTransform:
    def test_scheme_rewritten(self):
        kw = _prepare_engine_kwargs("postgresql://u:p@h/db")
        assert str(kw["url"]) == "postgresql+asyncpg://u:p@h/db"

    def test_no_sslmode_is_plaintext_not_opportunistic_tls(self):
        assert _prepare_engine_kwargs("postgresql://u:p@h/db")["connect_args"] == {"ssl": False}
        kw = _prepare_engine_kwargs("postgresql://u:p@h/db?sslmode=disable")
        assert (str(kw["url"]), kw["connect_args"]) == ("postgresql+asyncpg://u:p@h/db", {"ssl": False})

    def test_verify_full_checks_the_chain_and_the_host_name(self):
        kw = _prepare_engine_kwargs("postgresql://u:p@h/db?sslmode=verify-full&application_name=x")
        ctx = kw["connect_args"]["ssl"]
        assert isinstance(ctx, ssl.SSLContext)
        assert (ctx.verify_mode, ctx.check_hostname) == (ssl.CERT_REQUIRED, True)
        assert str(kw["url"]) == "postgresql+asyncpg://u:p@h/db?application_name=x"

    def test_verify_ca_checks_the_chain_but_not_the_host_name(self):
        ctx = _prepare_engine_kwargs("postgresql://u:p@h/db?sslmode=verify-ca")["connect_args"]["ssl"]
        assert isinstance(ctx, ssl.SSLContext)
        assert (ctx.verify_mode, ctx.check_hostname) == (ssl.CERT_REQUIRED, False)

    def test_sslrootcert_is_the_trust_anchor(self, tmp_path):
        """A private CA's certificate, as libpq's sslrootcert=. Mutation: ignore the parameter."""
        ca = tmp_path / "root.crt"
        ca.write_bytes(_self_signed_ca_pem())
        kw = _prepare_engine_kwargs(f"postgresql://u:p@h/db?sslmode=verify-full&sslrootcert={ca}")
        ctx = kw["connect_args"]["ssl"]
        assert isinstance(ctx, ssl.SSLContext)
        assert [c["subject"] for c in ctx.get_ca_certs()] == [((("commonName", "bfx-test-ca"),),)]
        assert "sslrootcert" not in str(kw["url"])


class TestEnginePoolConfig:
    def test_engine_has_pool_pre_ping_enabled(self):
        from bfx_funding_bot.core.db import make_engine
        from bfx_funding_bot.core.settings import Settings
        s = Settings.model_construct(database_url="postgresql://u:p@h/db")
        engine = make_engine(s)
        assert engine.pool._pre_ping is True

    def test_engine_has_pool_recycle_set(self):
        from bfx_funding_bot.core.db import make_engine
        from bfx_funding_bot.core.settings import Settings
        s = Settings.model_construct(database_url="postgresql://u:p@h/db")
        engine = make_engine(s)
        assert engine.pool._recycle == 600


class TestMakeAsyncEngineFromUrl:
    """5/21 chaos regression — daemon.py:614 bypassed _prepare_engine_kwargs
    + D3 pool config since Phase 4.1 (`26b059d`). Helper unifies the asyncpg
    engine construction path so daemon + make_engine share one source of truth.
    """

    def test_a_libpq_url_gets_the_transform_and_the_pool_config(self):
        from bfx_funding_bot.core.db import make_async_engine_from_url
        engine = make_async_engine_from_url("postgresql://u:p@h/db?sslmode=disable")
        assert str(engine.url).startswith("postgresql+asyncpg://")
        assert "sslmode" not in str(engine.url)
        assert engine.pool._pre_ping is True
        assert engine.pool._recycle == 600
