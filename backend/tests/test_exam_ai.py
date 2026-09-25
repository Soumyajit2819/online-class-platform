import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.exam_ai.openrouter import OpenRouterEvaluator, validate_model_result
from app.exam_ai.provider import EvaluationFailure, EvaluationInput


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.payload = payload or {'choices': [{'message': {'content': json.dumps({
            'marks': 4, 'max_marks': 5, 'confidence': .91,
            'reason': 'Correct main idea; one detail is missing.', 'manual_required': False,
        })}}]}
    def json(self): return self.payload


class MockClient:
    response = FakeResponse()
    error = None
    request = None
    def __init__(self, **kwargs): self.kwargs = kwargs
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return None
    async def post(self, url, **kwargs):
        type(self).request = (url, kwargs)
        if type(self).error: raise type(self).error
        return type(self).response


@pytest.fixture(autouse=True)
def reset_client():
    MockClient.response = FakeResponse(); MockClient.error = None; MockClient.request = None


def item(answer='A correct answer.'):
    return EvaluationInput('Science', 'Explain the concept.', 5, answer, 'Reference answer.', 'Award marks for correct reasoning.')


@pytest.mark.asyncio
async def test_openrouter_constructs_restricted_request_and_keeps_key_out_of_payload():
    evaluator = OpenRouterEvaluator(api_key='secret-test-key', model='qwen/test', client_factory=MockClient)
    result = await evaluator.evaluate(item())
    url, request = MockClient.request
    assert url == 'https://openrouter.ai/api/v1/chat/completions'
    assert request['headers']['Authorization'] == 'Bearer secret-test-key'
    serialized = json.dumps(request['json'])
    assert 'secret-test-key' not in serialized
    assert request['json']['model'] == 'qwen/test'
    assert request['json']['response_format'] == {'type': 'json_object'}
    assert result.marks == 4 and result.confidence == .91
    assert 'password' not in serialized and 'google' not in serialized


@pytest.mark.asyncio
async def test_provider_supports_bengali_and_banglish_answers():
    evaluator = OpenRouterEvaluator(api_key='test', client_factory=MockClient)
    await evaluator.evaluate(item('বাংলা উত্তর এবং Banglish uttor'))
    body = MockClient.request[1]['json']
    assert 'Bengali' in body['messages'][0]['content']
    assert 'Banglish' in body['messages'][0]['content']
    assert 'বাংলা উত্তর' in body['messages'][1]['content']


@pytest.mark.asyncio
async def test_valid_structured_response_is_parsed():
    result = await OpenRouterEvaluator(api_key='test', client_factory=MockClient).evaluate(item())
    assert (result.marks, result.max_marks, result.confidence, result.manual_required) == (4, 5, .91, False)


@pytest.mark.parametrize('content', ['not json', '{"marks": 4}', '```json\n{"marks":4}\n```'])
def test_malformed_or_incomplete_json_is_rejected(content):
    with pytest.raises(EvaluationFailure):
        validate_model_result(content, 5)


@pytest.mark.parametrize('data', [
    {'marks': -1, 'max_marks': 5, 'confidence': .8, 'reason': 'why', 'manual_required': False},
    {'marks': 6, 'max_marks': 5, 'confidence': .8, 'reason': 'why', 'manual_required': False},
    {'marks': True, 'max_marks': 5, 'confidence': .8, 'reason': 'why', 'manual_required': False},
    {'marks': 4, 'max_marks': 4, 'confidence': .8, 'reason': 'why', 'manual_required': False},
    {'marks': 4, 'max_marks': 5, 'confidence': 1.1, 'reason': 'why', 'manual_required': False},
    {'marks': 4, 'max_marks': 5, 'confidence': .8, 'reason': '', 'manual_required': False},
    {'marks': 4, 'max_marks': 5, 'confidence': .8, 'reason': 'why', 'manual_required': 'false'},
])
def test_invalid_marks_confidence_max_or_required_fields_force_manual_review(data):
    with pytest.raises(EvaluationFailure):
        validate_model_result(json.dumps(data), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(('status','retryable'), [(401,False),(403,False),(429,True),(500,True),(503,True)])
async def test_openrouter_handles_auth_rate_limit_and_provider_failures(status, retryable):
    MockClient.response = FakeResponse(status=status)
    with pytest.raises(EvaluationFailure) as raised:
        await OpenRouterEvaluator(api_key='test', client_factory=MockClient).evaluate(item())
    assert raised.value.retryable is retryable
    assert raised.value.status_code == status


@pytest.mark.asyncio
async def test_openrouter_timeout_and_network_errors_are_retryable():
    MockClient.error = httpx.ReadTimeout('slow')
    with pytest.raises(EvaluationFailure) as timeout:
        await OpenRouterEvaluator(api_key='test', client_factory=MockClient).evaluate(item())
    assert timeout.value.retryable
    MockClient.error = httpx.ConnectError('offline')
    with pytest.raises(EvaluationFailure) as network:
        await OpenRouterEvaluator(api_key='test', client_factory=MockClient).evaluate(item())
    assert network.value.retryable


@pytest.mark.asyncio
async def test_refusal_is_not_turned_into_a_mark():
    MockClient.response = FakeResponse(payload={'choices': [{'finish_reason': 'stop', 'message': {'refusal': 'No', 'content': ''}}]})
    with pytest.raises(EvaluationFailure):
        await OpenRouterEvaluator(api_key='test', client_factory=MockClient).evaluate(item())
