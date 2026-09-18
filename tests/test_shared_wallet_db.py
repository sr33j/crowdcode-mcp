"""Real PostgreSQL regression tests; never use the application's DATABASE_URL."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
import os

import psycopg
from psycopg.rows import dict_row
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode.identity import build_identity, create_service_from_identity, resolve_service
from crowdcode.payments import canonical_review_payload, BASE_USDC_ADDRESS, ERC20_TRANSFER_TOPIC

SCHEMA = Path(__file__).parents[1] / 'supabase/schema.sql'
PAYEE = '0x' + 'ab' * 20


@pytest.fixture
def db():
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('TEST_DATABASE_URL is required for PostgreSQL integration tests')
    schema = 'identity_test_' + uuid4().hex
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f'create schema {schema}')
        conn.execute(f'set search_path to {schema}')
        conn.execute(SCHEMA.read_text())
        try:
            yield conn, schema, url
        finally:
            conn.execute(f'drop schema {schema} cascade')


def identity(path, provider='x402', payee=PAYEE):
    return build_identity(api_endpoint=f'https://provider.example/{path}',
                          payment_provider=provider, payment_target_ref=payee)


def test_shared_wallet_registration_and_aliases(db):
    conn, _, _ = db
    with conn.transaction():
        first = create_service_from_identity(conn, identity('dossier'))
        second = create_service_from_identity(conn, identity('video'))
    assert first.row['id'] != second.row['id']
    for original, path in [(first, 'dossier'), (second, 'video')]:
        result = resolve_service(conn, identity(path, 'mppx', PAYEE.upper().replace('0X', '0x')))
        assert result.error is None
        assert result.row['id'] == original.row['id']
    wallet_only = resolve_service(conn, build_identity(payment_provider='x402', payment_target_ref=PAYEE))
    assert 'shared by multiple services' in wallet_only.error
    assert conn.execute("select count(*) as n from service_identifiers where identifier_type='payment_target'").fetchone()['n'] == 2
    assert resolve_service(conn, identity('video', payee='0x'+'99'*20)).error == 'service identity conflict'


def test_old_schema_rollout_and_idempotent_backfill(db):
    conn, _, _ = db
    conn.execute('drop index service_identifiers_service_unique')
    conn.execute('drop index service_identifiers_product_unique')
    conn.execute('alter table service_identifiers add constraint service_identifiers_identifier_type_identifier_value_key unique(identifier_type, identifier_value)')
    with conn.transaction():
        first = create_service_from_identity(conn, identity('dossier'))
        second = create_service_from_identity(conn, identity('video'))
    # The new backend also works before migration, using canonical payees.
    assert first.row['id'] != second.row['id']
    assert 'shared by multiple services' in resolve_service(conn, build_identity(payment_provider='x402', payment_target_ref=PAYEE)).error
    conn.execute(SCHEMA.read_text())
    conn.execute(SCHEMA.read_text())
    assert conn.execute("select count(*) as n from service_identifiers where identifier_type='payment_target'").fetchone()['n'] == 2
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("insert into service_identifiers(service_id,identifier_type,identifier_value) values(%s,'api_endpoint',%s)", (second.row['id'], identity('dossier').api_endpoint))


def test_concurrent_first_reviews_register_one_endpoint(db):
    _, schema, url = db
    def register(provider):
        with psycopg.connect(url, row_factory=dict_row) as conn:
            conn.execute(f'set search_path to {schema}')
            return create_service_from_identity(conn, identity('video', provider)).row['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, ['x402', 'mppx']))
    assert len(set(results)) == 1


def test_signed_reviews_stay_on_separate_services(db, monkeypatch):
    from crowdcode import server, payments
    conn, _, url = db
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.delenv('X402_USDC_ADDRESS', raising=False)
    @contextmanager
    def connect():
        yield conn
    monkeypatch.setattr(server, 'connect', connect)
    monkeypatch.setattr(server, 'redact_texts', lambda texts, **_: texts)
    monkeypatch.setattr(server, 'redaction_enabled', lambda: True)
    account = Account.from_key('0x' + '42'*32)
    topic = lambda address: '0x' + '0'*24 + address[2:].lower()
    monkeypatch.setattr(payments, '_rpc_transaction_receipt', lambda *_: {
        'status':'0x1', 'blockNumber':'0x10', 'logs':[{
            'address': BASE_USDC_ADDRESS,
            'topics':[ERC20_TRANSFER_TOPIC,topic(account.address),topic(PAYEE)],
            'data':hex(200000),
        }],
    })
    ids = []
    for path, rating, tx in [('dossier', 2, '11'), ('video', 5, '22')]:
        ident, reference, reason = identity(path), '0x'+tx*32, f'Result of {path}'
        message = canonical_review_payload(identity=ident,rating=rating,reason=reason,payment_reference=reference)
        result = server.review_service(api_endpoint=ident.api_endpoint,payment_provider='x402',payment_target_ref=PAYEE,
            payment_reference=reference,rating=rating,reason=reason,reviewer_wallet=account.address,
            review_signature='0x'+account.sign_message(encode_defunct(text=message)).signature.hex())
        assert result['accepted'], result
        assert result['payment_verified'], result
        ids.append(result['service_id'])
    assert ids[0] != ids[1]
    rows = conn.execute('select service_id,rating from reviews order by rating').fetchall()
    assert [(r['service_id'],r['rating']) for r in rows] == [(ids[0],2),(ids[1],5)]
