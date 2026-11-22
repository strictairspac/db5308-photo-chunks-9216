import hashlib
import hmac
import secrets

from fastapi.testclient import TestClient

from src.config.settings import engine
from src.main import app
from src.models.tables import schema, users
from src.services.security import hash_password


def make_user(conn, username, role):
    conn.execute(users.insert().values(username=username, role=role,
                                       password_hash=hash_password('Demo-Walkthrough-2026')))


def setup_module():
    schema.drop_all(engine)
    schema.create_all(engine)
    with engine.begin() as conn:
        make_user(conn, 'operator', 'operator')
        make_user(conn, 'viewer', 'viewer')


def token(client, username):
    response = client.post('/login', json={'username': username, 'password': 'Demo-Walkthrough-2026'})
    assert response.status_code == 200, response.text
    return response.json()['token']


def auth(value):
    return {'Authorization': f'Bearer {value}'}


def block(char, size):
    return {'address': char * 64, 'bytes': size}


def test_end_to_end_billing():
    with TestClient(app) as client:
        operator = auth(token(client, 'operator'))
        viewer = auth(token(client, 'viewer'))

        for key, group, char, size in [('r1', 'A', 'a', 8), ('r2', 'B', 'a', 8), ('r3', 'A', 'b', 5)]:
            response = client.post('/billing/records', headers=operator, json={
                'month': '2026-11', 'production_group': group, 'record_key': key,
                'blocks': [block(char, size)]})
            assert response.status_code == 201, response.text

        # 观察员可读不可写。
        assert client.post('/billing/records', headers=viewer, json={
            'month': '2026-11', 'production_group': 'A', 'record_key': 'x',
            'blocks': [block('c', 1)]}).status_code == 403

        closed = client.post('/billing/periods/2026-11/close', headers=operator)
        assert closed.status_code == 201, closed.text
        settlement = closed.json()
        assert settlement['kind'] == 'original'
        totals = {row['production_group']: row['total_bytes'] for row in settlement['bills']}
        assert totals == {'A': 9, 'B': 4}

        original_bill = settlement['bills'][0]
        before = client.get(f'/billing/bills/{original_bill["id"]}', headers=viewer).json()

        # 关账后补入迟到记录：自动产生差额账单。
        response = client.post('/billing/records', headers=operator, json={
            'month': '2026-11', 'production_group': 'B', 'record_key': 'r4',
            'blocks': [block('b', 5)]})
        assert response.status_code == 201, response.text
        settlements = client.get('/billing/settlements?month=2026-11', headers=viewer).json()
        assert [row['kind'] for row in settlements] == ['adjustment', 'original']
        adjustment = settlements[0]
        delta = {row['production_group']: row['total_bytes'] for row in adjustment['bills']}
        assert delta == {'A': -2, 'B': 2}

        # 原账单依据未变，可回查。
        assert client.get(f'/billing/bills/{original_bill["id"]}', headers=viewer).json() == before
        assert client.get(f'/billing/settlements/{settlement["id"]}', headers=viewer).json() == settlement

        # 撤销迟到记录：再出一张冲正差额。
        record = client.get('/billing/records?month=2026-11', headers=viewer).json()[0]
        reversal = client.post(f'/billing/records/{record["id"]}/reverse', headers=operator,
                               json={'note': '重复上报'})
        assert reversal.status_code == 200, reversal.text
        summary = client.get('/billing/summary?month=2026-11', headers=viewer).json()
        assert all(row['matched'] for row in summary['groups'])
        assert {row['production_group']: row['actual_bytes'] for row in summary['groups']} == {'A': 9, 'B': 4}

        # 重试关账/结算不重复出账。
        again = client.post('/billing/periods/2026-11/close', headers=operator)
        assert again.status_code == 201
        count = len(client.get('/billing/settlements?month=2026-11', headers=viewer).json())
        client.post('/billing/periods/2026-11/settle', headers=operator)
        assert len(client.get('/billing/settlements?month=2026-11', headers=viewer).json()) == count
