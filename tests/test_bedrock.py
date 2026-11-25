import io
import json
import unittest
from unittest.mock import Mock, patch
import proxy
import bedrock_backend as bedrock
from botocore.exceptions import ClientError


class BedrockTests(unittest.TestCase):
    def test_opus_aliases(self):
        for name in ('claude-opus-5-5', 'claude-opus-5.5', 'opus-5-5', 'opus-5.5', 'opus5.5', 'opus'):
            self.assertEqual(bedrock.model_id(name), 'us.anthropic.claude-opus-5-5')

    def setUp(self):
        self.client = proxy.app.test_client()
        self.body = {'model': 'claude-sonnet-4-6', 'max_tokens': 10,
                     'system': 'Keep this instruction',
                     'tools': [{'name': 'exec', 'input_schema': {'type': 'object'}}],
                     'messages': [{'role': 'user', 'content': 'hi'}]}

    def post(self):
        with patch.object(proxy, 'UPSTREAM_PROVIDER', 'bedrock'), patch.object(proxy, 'PROXY_API_KEY', ''):
            return self.client.post('/v1/messages', json=self.body)

    def test_preserves_system_and_tools_without_oauth(self):
        upstream = Mock()
        upstream.invoke_model.return_value = {'body': io.BytesIO(b'{"type":"message","content":[]}')}
        with patch.object(bedrock, 'client', return_value=upstream), patch.object(proxy, 'get_access_token') as oauth:
            self.assertEqual(self.post().status_code, 200)
        oauth.assert_not_called()
        payload = json.loads(upstream.invoke_model.call_args.kwargs['body'])
        self.assertEqual(payload['system'], self.body['system'])
        self.assertEqual(payload['tools'], self.body['tools'])
        self.assertNotIn('model', payload)

    def test_stream_and_close(self):
        self.body['stream'] = True
        source = Mock()
        source.__iter__ = Mock(return_value=iter([{'chunk': {'bytes': b'{"type":"message_stop"}'}}]))
        upstream = Mock()
        upstream.invoke_model_with_response_stream.return_value = {'body': source}
        with patch.object(bedrock, 'client', return_value=upstream):
            response = self.post()
            self.assertIn(b'event: message_stop', response.data)
            response.close()
        source.close.assert_called()

    def test_context_beta_header_is_translated(self):
        self.body['context_management'] = {'edits': [{'type': 'clear_thinking_20251015', 'keep': 'all'}]}
        upstream = Mock()
        upstream.invoke_model.return_value = {'body': io.BytesIO(b'{}')}
        with patch.object(bedrock, 'client', return_value=upstream), patch.object(proxy, 'UPSTREAM_PROVIDER', 'bedrock'), patch.object(proxy, 'PROXY_API_KEY', ''):
            result = self.client.post('/v1/messages', json=self.body, headers={
                'anthropic-beta': 'claude-code-20250219,context-management-2025-06-27'})
        self.assertEqual(result.status_code, 200)
        sent = json.loads(upstream.invoke_model.call_args.kwargs['body'])
        self.assertEqual(sent['context_management'], self.body['context_management'])
        self.assertEqual(sent['anthropic_beta'], ['context-management-2025-06-27'])

    def test_new_beta_features_are_translated_and_preserved(self):
        self.body['messages'][0]['output_config'] = {'effort': 'low'}
        self.body['thinking'] = {'type': 'adaptive', 'display': 'updates'}
        self.body['anthropic_beta'] = ['per-turn-control-2026-07-01']
        upstream = Mock()
        upstream.invoke_model.return_value = {'body': io.BytesIO(b'{}')}
        with patch.object(bedrock, 'client', return_value=upstream), patch.object(proxy, 'UPSTREAM_PROVIDER', 'bedrock'), patch.object(proxy, 'PROXY_API_KEY', ''):
            result = self.client.post('/v1/messages', json=self.body, headers={
                'anthropic-beta': 'per-turn-control-2026-07-01,thinking-display-updates-2026-08-18'})
        self.assertEqual(result.status_code, 200)
        sent = json.loads(upstream.invoke_model.call_args.kwargs['body'])
        self.assertEqual(sent['anthropic_beta'], ['per-turn-control-2026-07-01', 'thinking-display-updates-2026-08-18'])
        self.assertEqual(sent['messages'], self.body['messages'])
        self.assertEqual(sent['thinking'], self.body['thinking'])

    def test_access_denied(self):
        upstream = Mock()
        upstream.invoke_model.side_effect = ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'Denied'}}, 'InvokeModel')
        with patch.object(bedrock, 'client', return_value=upstream):
            self.assertEqual(self.post().status_code, 403)
