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


@pytest.fixture
def review_backend(db, monkeypatch):
    from crowdcode import server
    conn, schema, url = db
    monkeypatch.setenv('DATABASE_URL', url)
    @contextmanager
    def connect():
        with psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}") as connection:
            yield connection
    monkeypatch.setattr(server, 'connect', connect)
    monkeypatch.setattr(server, 'redact_texts', lambda texts, **_: texts)
    monkeypatch.setattr(server, 'redaction_enabled', lambda: True)
    account = Account.from_key('0x' + '42'*32)
    def submit(ident, nonce, rating=2, reason='Endpoint returned HTTP 500', reference=None):
        effective = resolve_service(conn, ident).identity or ident
        message = canonical_review_payload(identity=effective, rating=rating, reason=reason,
                                           review_nonce=nonce, payment_reference=reference)
        return server.review_service(api_endpoint=ident.api_endpoint, service_id=ident.service_id,
            payment_provider=ident.payment_provider, payment_target_ref=ident.payment_target_ref,
            rating=rating, reason=reason, review_nonce=nonce, payment_reference=reference,
            reviewer_wallet=account.address,
            review_signature='0x'+account.sign_message(encode_defunct(text=message)).signature.hex())
    return conn, server, account, submit


def test_unpaid_review_stored_once_visible_and_scored(review_backend):
    from crowdcode.cron import _summary_input_reviews
    from crowdcode.payments import utc_now
    conn, server, account, submit = review_backend
    # An unverified caller-supplied payee must not become the product's payee.
    ident = identity('unpaid')
    first = submit(ident, 'attempt_123')
    assert first['accepted'] and first['signature_verified'] and not first['payment_verified'], first
    assert first['payment_verification_level'] == 'signature_only'
    service = conn.execute('select * from services where id=%s', (first['service_id'],)).fetchone()
    assert service['payment_target_ref'] is None and service['payment_provider'] is None
    retry = submit(ident, 'attempt_123')
    assert retry['accepted'] and retry['review_id'] == first['review_id'], retry
    conflict = submit(ident, 'attempt_123', reason='Different review')
    assert conflict['error_code'] == 'review_nonce_used'
    assert conn.execute('select count(*) as n from reviews').fetchone()['n'] == 1
    rows = _summary_input_reviews(conn, first['service_id'], {}, utc_now())
    assert len(rows) == 1 and not rows[0]['payment_verified']
    conn.execute('update wallet_users set is_seed=true where wallet_address=%s', (account.address.lower(),))
    second = submit(ident, 'attempt_456', rating=3)
    assert second['accepted'], second
    score = server.get_service_score(api_endpoint=ident.api_endpoint)
    assert score['num_reviews'] == 2 and score['num_verified_reviews'] == 0
    assert score['n_eff'] > 0
    assert len(score['recent_reviews']) == 2
    assert all(not row['payment_verified'] for row in score['recent_reviews'])
    assert score['recent_reviews'][0]['reason'] == 'Endpoint returned HTTP 500'
    # Reapplying the full schema does not fabricate payment evidence.
    conn.execute(SCHEMA.read_text())
    assert conn.execute('select count(*) as n from reviews where payment_reference is null').fetchone()['n'] == 2


def test_paid_review_after_unpaid_endpoint_retains_one_product(review_backend, monkeypatch):
    from crowdcode import payments
    conn, _, account, submit = review_backend
    ident = identity('first-unpaid')
    unpaid = submit(build_identity(api_endpoint=ident.api_endpoint), 'attempt_unpaid')
    assert unpaid['accepted'], unpaid
    topic = lambda address: '0x' + '0'*24 + address[2:].lower()
    monkeypatch.setattr(payments, '_rpc_transaction_receipt', lambda *_: {
        'status':'0x1', 'blockNumber':'0x10', 'logs':[{
            'address': BASE_USDC_ADDRESS,
            'topics':[ERC20_TRANSFER_TOPIC,topic(account.address),topic(PAYEE)],
            'data':hex(200000),
        }],
    })
    paid = submit(ident, None, rating=5, reference='0x'+'55'*32)
    assert paid['accepted'] and paid['payment_verified'], paid
    assert paid['service_id'] == unpaid['service_id']
    stored = conn.execute('select payment_target_ref from services where id=%s', (paid['service_id'],)).fetchone()
    assert stored['payment_target_ref'] == PAYEE
    assert resolve_service(conn, identity('first-unpaid', payee='0x'+'99'*20)).error == 'service identity conflict'


def test_concurrent_unpaid_retries_have_one_row(review_backend, db):
    from crowdcode.payments import canonical_review_payload
    _, server, account, _ = review_backend
    ident = build_identity(api_endpoint='https://provider.example/concurrent-unpaid')
    message = canonical_review_payload(identity=ident, rating=2, reason='HTTP 500', review_nonce='attempt_race')
    args = dict(api_endpoint=ident.api_endpoint, rating=2, reason='HTTP 500', review_nonce='attempt_race',
                reviewer_wallet=account.address,
                review_signature='0x'+account.sign_message(encode_defunct(text=message)).signature.hex())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: server.review_service(**args), range(2)))
    # A request resolving after registration can ask the caller to re-sign;
    # requests resolving before it must deduplicate at the unique index.
    assert any(result.get('accepted') for result in results), results
    assert all(result.get('accepted') or result.get('expected_message') for result in results), results
    conn, _, _ = db
    assert conn.execute('select count(*) as n from reviews').fetchone()['n'] == 1
