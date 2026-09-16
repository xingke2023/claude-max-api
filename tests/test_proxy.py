import copy
import json
import subprocess
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

with patch('subprocess.check_output', return_value='2.1.133'):
    import proxy


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.auth = patch.object(proxy, 'PROXY_API_KEY', 'test-secret')
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.client = proxy.app.test_client()
        self.headers = {'x-api-key': 'test-secret'}
        self.body = {'model': 'test', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': 'hi'}]}

    def post(self, body):
        return self.client.post('/v1/messages', json=body, headers=self.headers)

    def test_authentication(self):
        self.assertEqual(self.client.get('/health').status_code, 401)
        self.assertEqual(self.client.get('/health', headers={'x-api-key': 'wrong'}).status_code, 401)
        with patch.object(proxy, 'load_credentials', return_value={'expiresAt': (time.time()+600)*1000}):
            self.assertEqual(self.client.get('/health', headers={'Authorization': 'Bearer test-secret'}).status_code, 200)

    def test_invalid_requests_do_not_use_credentials(self):
        with patch.object(proxy, 'get_access_token') as token:
            for body in [[], {}, {**self.body, 'stream': 'yes'}, {**self.body, 'messages': [{'role': [], 'content': 'hi'}]}, {**self.body, 'tools': [None]},
                         {**self.body, 'system': [None]}, {**self.body, 'messages': [{'role': 'user', 'content': [None]}]}]:
                self.assertEqual(self.post(body).status_code, 400)
            token.assert_not_called()

    def test_client_message_extensions_reach_upstream_unchanged(self):
        # Do not reject or silently rewrite newer client roles/content formats.
        for message in [
            {'role': 'assistant', 'content': None},
            {'role': 'assistant', 'content': {'type': 'text', 'text': 'hello'}},
            {'role': 'system', 'content': 'client extension'},
            {'role': 'assistant'},
        ]:
            with self.subTest(message=message):
                body = copy.deepcopy(self.body)
                body['messages'].append(message)
                resp = Mock(status_code=200, headers={'content-type': 'application/json'})
                resp.content = b'{"type":"message","content":[]}'
                with patch.object(proxy, 'get_access_token', return_value='oauth'), patch.object(proxy.requests, 'post', return_value=resp) as post:
                    self.assertEqual(self.post(body).status_code, 200)
                sent = json.loads(post.call_args.kwargs['data'])
                self.assertEqual(sent['messages'][-1], message)

    def test_upstream_validation_error_is_preserved(self):
        resp = Mock(status_code=400, headers={'content-type': 'application/json'})
        resp.content = b'{"type":"error","error":{"type":"invalid_request_error","message":"messages.1: unsupported content"}}'
        with patch.object(proxy, 'get_access_token', return_value='oauth'), patch.object(proxy.requests, 'post', return_value=resp):
            body = copy.deepcopy(self.body)
            body['messages'].append({'role': 'assistant', 'content': None})
            result = self.post(body)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json['error']['message'], 'messages.1: unsupported content')

    def test_complete_tool_roundtrip(self):
        body = copy.deepcopy(self.body)
        body['tools'] = [{'name': 'exec', 'input_schema': {}}, {'name': 'feishu_doc', 'input_schema': {}}]
        body['messages'] += [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't', 'name': 'exec', 'input': {'name': 'Bash'}}]},
                             {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't', 'content': 'ok'}]}]
        body['tool_choice'] = {'type': 'tool', 'name': 'exec'}
        reverse = proxy.replace_tools(body)
        self.assertEqual([t['name'] for t in body['tools']], ['Bash', 'feishu_doc'])
        self.assertEqual(body['messages'][1]['content'][0]['name'], 'Bash')
        self.assertEqual(body['tool_choice']['name'], 'Bash')
        data = {'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'name': 'Bash'}}]}
        result = proxy.remap_response(data, reverse)['content'][0]
        self.assertEqual(result['name'], 'exec')
        self.assertEqual(result['input']['name'], 'Bash')

    def test_native_tools_and_collisions(self):
        body = {**self.body, 'tools': [{'name': 'Bash'}]}
        self.assertEqual(proxy.replace_tools(body), {'Bash': 'Bash'})
        with self.assertRaises(ValueError):
            proxy.replace_tools({**self.body, 'tools': [{'name': 'exec'}, {'name': 'Bash'}]})

    def test_stream_split_at_every_byte(self):
        event = {'type': 'content_block_start', 'content_block': {'type': 'tool_use', 'name': 'Bash', 'input': {'name': 'Bash', 'text': '你好'}}}
        raw = ('event: content_block_start\r\ndata: '+json.dumps(event, ensure_ascii=False)+'\r\n\r\n').encode()
        resp = Mock()
        resp.iter_content.return_value = [raw[i:i+1] for i in range(len(raw))]
        output = b''.join(proxy.stream_events(resp, {'Bash': 'exec'})).decode()
        result = json.loads(output.split('data: ')[1])['content_block']
        self.assertEqual(result['name'], 'exec')
        self.assertEqual(result['input'], {'name': 'Bash', 'text': '你好'})
        resp.close.assert_called_once()

    def test_stream_disconnect_and_truncation(self):
        for chunks in [[b'data: {'], [b'event: ping\ndata: {}\n\n']]:
            resp = Mock()
            resp.iter_content.return_value = iter(chunks)
            stream = proxy.stream_events(resp, {})
            first = next(stream)
            if chunks == [b'data: {']:
                self.assertIn(b'event: error', first)
            stream.close()
            resp.close.assert_called_once()

    def test_upstream_failures(self):
        with patch.object(proxy, 'get_access_token', return_value='oauth'):
            for exc, status in [(proxy.requests.Timeout(), 504), (proxy.requests.ConnectionError(), 502)]:
                with patch.object(proxy.requests, 'post', side_effect=exc):
                    self.assertEqual(self.post(self.body).status_code, status)
        with patch.object(proxy, 'get_access_token', side_effect=proxy.CredentialError()):
            self.assertEqual(self.post(self.body).status_code, 503)

    def test_nonstream_and_stream_error_responses(self):
        with patch.object(proxy, 'get_access_token', return_value='oauth'):
            for stream, status in [(False, 200), (True, 429)]:
                resp = Mock(status_code=status, headers={'content-type': 'application/json', 'retry-after': '30'})
                resp.content = b'{"content":[{"type":"tool_use","name":"Bash","input":{"name":"Bash"}}]}'
                with patch.object(proxy.requests, 'post', return_value=resp):
                    result = self.post({**self.body, 'stream': stream, 'tools': [{'name': 'exec'}]})
                self.assertEqual(result.status_code, status)
                self.assertEqual(result.json['content'][0]['name'], 'exec')
                self.assertEqual(result.headers['retry-after'], '30')
                resp.close.assert_called_once()

    def test_refresh_serialized(self):
        state = {'expiresAt': 0, 'accessToken': 'old'}
        def refresh(*args, **kwargs):
            state.update(expiresAt=(time.time()+3600)*1000, accessToken='new')
        with patch.object(proxy, 'load_credentials', side_effect=lambda: state.copy()), patch.object(proxy.subprocess, 'run', side_effect=refresh) as run:
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(lambda _: proxy.get_access_token(), range(8))), ['new']*8)
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs['timeout'], proxy.REFRESH_TIMEOUT)

    def test_refresh_timeout(self):
        with patch.object(proxy, 'load_credentials', return_value={'expiresAt': 0}), patch.object(proxy.subprocess, 'run', side_effect=subprocess.TimeoutExpired('claude', 60)):
            with self.assertRaises(proxy.CredentialError):
                proxy.get_access_token()

    def test_system_modes(self):
        body = {**copy.deepcopy(self.body), 'system': [{'type': 'text', 'text': 'instructions', 'cache_control': {'type': 'ephemeral'}}]}
        with patch.object(proxy, 'SYSTEM_PROMPT_MODE', 'preserve'):
            self.assertEqual(json.loads(proxy.inject_system_and_cch(copy.deepcopy(body))), body)
        with patch.object(proxy, 'SYSTEM_PROMPT_MODE', 'legacy'):
            result = json.loads(proxy.inject_system_and_cch(body))
            self.assertIn('instructions', result['messages'][0]['content'])
            self.assertIn('Claude Code', result['system'][1]['text'])


if __name__ == '__main__':
    unittest.main()
