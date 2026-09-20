from pathlib import Path
from unittest.mock import Mock
from streamlit.testing.v1 import AppTest
from governance.demo import demo_client

APP = Path(__file__).resolve().parents[1] / 'streamlit' / 'governance_app.py'


def test_demo_navigation_and_inherited_permissions(monkeypatch):
    monkeypatch.setenv('GOVERNANCE_DEMO', 'true')
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not app.exception
    app.radio(key='gov_kind').set_value('Table').run()
    assert not app.exception
    assert app.subheader[0].value == 'demo_governance.curated.customers'
    assert any('Inherited from' in frame.value.columns for frame in app.dataframe)
    assert not any(b.label == 'Áp dụng thay đổi' for b in app.button)
    app.radio(key='gov_kind').set_value('Function').run()
    assert not app.exception
    assert 'normalize_email' in app.subheader[0].value


def test_review_then_apply_calls_sdk_once(monkeypatch):
    monkeypatch.setenv('GOVERNANCE_DEMO', 'false')
    monkeypatch.setenv('GOVERNANCE_LOCAL', 'false')
    monkeypatch.setenv('GOVERNANCE_CATALOGS', 'demo_governance')
    monkeypatch.setenv('GOVERNANCE_ENABLE_WRITES', 'true')
    monkeypatch.setenv('GOVERNANCE_ADMIN_EMAILS', 'admin@example.com')
    import governance.ui as ui
    client = demo_client()
    client.grants.update = Mock()
    monkeypatch.setattr(ui, 'make_client', lambda settings: client)
    monkeypatch.setattr(ui, 'actor_from_headers', lambda headers: 'admin@example.com')
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio(key='gov_kind').set_value('Table').run()
    assert not app.exception
    next(x for x in app.text_input if x.label == 'Principal').set_value('analysts')
    app.multiselect[0].set_value(['MODIFY'])
    app.text_area[0].set_value('Approved ticket TEST-1')
    next(b for b in app.button if b.label == 'Xem trước thay đổi').click().run()
    assert not app.exception
    client.grants.update.assert_not_called()
    next(x for x in app.text_input if x.label == 'Nhập lại tên đầy đủ của đối tượng').set_value('demo_governance.curated.customers')
    next(b for b in app.button if b.label == 'Áp dụng thay đổi').click().run()
    assert not app.exception
    client.grants.update.assert_called_once()
    assert any('Databricks đã xác nhận' in message.value for message in app.success)
    app.run()
    client.grants.update.assert_called_once()
