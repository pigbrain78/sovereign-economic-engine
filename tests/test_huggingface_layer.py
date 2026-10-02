import json

import pytest
from fastapi.testclient import TestClient

from app.huggingface_layer import ModelPolicyViolation, catalog, select_model
from app.main import app

client = TestClient(app)


def verified_pricing():
    return {
        model.model_id: {
            'input_usd_per_1m': '0.10' if model.model_id.startswith('Qwen/') else '0.20',
            'output_usd_per_1m': '0.20' if model.model_id.startswith('Qwen/') else '0.40',
            'source': 'test_verified',
        }
        for model in catalog()
    }


def test_catalog_is_visible_but_unpriced_models_fail_closed(monkeypatch):
    monkeypatch.delenv('HF_MODEL_PRICING_JSON', raising=False)
    response = client.get('/hf/models')
    assert response.status_code == 200
    assert response.json()['source'] == 'huggingface_inference_providers'
    assert response.json()['models']
    assert all(model['pricing_verified'] is False for model in response.json()['models'])
    with pytest.raises(ModelPolicyViolation, match='verified pricing'):
        select_model(capabilities=['chat'], input_tokens=100, output_tokens=50, max_cost=1)


def test_verified_pricing_selects_by_budget_and_policy(monkeypatch):
    monkeypatch.setenv('HF_MODEL_PRICING_JSON', json.dumps(verified_pricing()))
    selected, cost, alternatives = select_model(capabilities=['chat', 'structured_output'], input_tokens=1000, output_tokens=500, max_cost=1, routing_policy='cheapest')
    assert selected.model_id == 'Qwen/Qwen2.5-7B-Instruct'
    assert cost > 0
    assert isinstance(alternatives, list)

    quote = client.post('/hf/quote', json={
        'capabilities': ['chat', 'structured_output'],
        'input_tokens': 1000,
        'output_tokens': 500,
        'max_cost': '1.00',
        'routing_policy': 'cheapest',
    })
    assert quote.status_code == 200
    assert quote.json()['execution_authorized'] is False
    assert quote.json()['wallet_reservation_required'] is True


def test_quote_rejects_unpriced_or_over_budget_models(monkeypatch):
    monkeypatch.delenv('HF_MODEL_PRICING_JSON', raising=False)
    blocked = client.post('/hf/quote', json={
        'capabilities': ['chat'], 'input_tokens': 100, 'output_tokens': 100,
        'max_cost': '1.00', 'routing_policy': 'cheapest',
    })
    assert blocked.status_code == 422

    monkeypatch.setenv('HF_MODEL_PRICING_JSON', json.dumps(verified_pricing()))
    over_budget = client.post('/hf/quote', json={
        'capabilities': ['chat'], 'input_tokens': 10_000_000, 'output_tokens': 10_000_000,
        'max_cost': '0.01', 'routing_policy': 'cheapest',
    })
    assert over_budget.status_code == 422


def test_provider_call_is_disabled_without_unsettled_chat_flag(monkeypatch):
    monkeypatch.setenv('HF_MODEL_PRICING_JSON', json.dumps(verified_pricing()))
    monkeypatch.delenv('HF_ALLOW_UNSETTLED_CHAT', raising=False)
    response = client.post('/hf/chat', json={
        'capabilities': ['chat'], 'input_tokens': 10, 'output_tokens': 10,
        'max_cost': '1.00', 'messages': [{'role': 'user', 'content': 'hello'}],
    })
    assert response.status_code == 403
    assert 'wallet reservation' in response.json()['detail']


def test_provider_status_never_exposes_token(monkeypatch):
    monkeypatch.setenv('HF_TOKEN', 'secret-test-token')
    monkeypatch.delenv('HF_MODEL_PRICING_JSON', raising=False)
    response = client.get('/hf/provider-status')
    assert response.status_code == 200
    body = response.json()
    assert body['token_configured'] is True
    assert 'secret-test-token' not in response.text
