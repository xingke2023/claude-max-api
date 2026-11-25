"""Bedrock InvokeModel adapter for the Anthropic Messages endpoint."""
import json
import os

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from flask import Response, request, stream_with_context


def client(timeout):
    return boto3.client('bedrock-runtime', region_name=os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')),
                        config=Config(connect_timeout=10, read_timeout=timeout, retries={'max_attempts': 0}))


def model_id(model):
    mapping = json.loads(os.environ.get('BEDROCK_MODEL_MAP', '{}'))
    if model in mapping:
        return mapping[model]
    if model.startswith(('arn:', 'us.', 'eu.', 'apac.', 'global.', 'anthropic.')):
        return model
    aliases = {
        'claude-opus-5-5': 'us.anthropic.claude-opus-5-5',
        'claude-opus-5.5': 'us.anthropic.claude-opus-5-5',
        'opus-5-5': 'us.anthropic.claude-opus-5-5',
        'opus-5.5': 'us.anthropic.claude-opus-5-5',
        'opus5.5': 'us.anthropic.claude-opus-5-5',
        'opus': 'us.anthropic.claude-opus-5-5',
        'claude-sonnet-4-6': 'us.anthropic.claude-sonnet-4-6',
        'claude-opus-4-6': 'us.anthropic.claude-opus-4-6-v1',
        'claude-haiku-4-5-20251001': 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
    }
    if model not in aliases:
        raise ValueError('Unknown Bedrock model; use a Bedrock model/profile ID or configure BEDROCK_MODEL_MAP')
    return aliases[model]


def error(message, status, kind='api_error'):
    return {'type': 'error', 'error': {'type': kind, 'message': message}}, status


def invoke(body, timeout):
    try:
        model = model_id(body['model'])
    except ValueError as exc:
        return error(str(exc), 400, 'invalid_request_error')
    payload = dict(body)
    streaming = payload.pop('stream', False)
    payload.pop('model')
    payload['anthropic_version'] = 'bedrock-2023-05-31'
    # Bedrock rejects Anthropic-only client metadata.
    payload.pop('metadata', None)
    # InvokeModel receives beta flags in JSON, rather than HTTP headers.
    supported_betas = {
        'context-management-2025-06-27', 'compact-2026-01-12', 'compact-2026-09-04',
        'fine-grained-tool-streaming-2025-05-14', 'interleaved-thinking-2025-05-14',
        'context-1m-2025-08-07', 'effort-2025-11-24',
        'per-turn-control-2026-07-01', 'thinking-display-updates-2026-08-18',
        'tool-search-tool-2025-10-19', 'tool-examples-2025-10-29',
    }
    betas = payload.get('anthropic_beta', [])
    if not isinstance(betas, list) or any(not isinstance(b, str) for b in betas):
        return error('anthropic_beta must be an array of strings', 400, 'invalid_request_error')
    betas = list(betas)
    betas.extend(b.strip() for b in request.headers.get('anthropic-beta', '').split(',')
                 if b.strip() in supported_betas)
    context = payload.get('context_management')
    if isinstance(context, dict):
        betas.append('context-management-2025-06-27')
        if any(isinstance(edit, dict) and edit.get('type') == 'compact_20260112'
               for edit in context.get('edits', [])):
            betas.append('compact-2026-01-12')
    if betas:
        payload['anthropic_beta'] = list(dict.fromkeys(betas))
    args = dict(modelId=model, body=json.dumps(payload).encode(), contentType='application/json', accept='application/json')
    try:
        upstream = client(timeout)
        result = upstream.invoke_model_with_response_stream(**args) if streaming else upstream.invoke_model(**args)
        source = result['body']
        if not streaming:
            try:
                return Response(source.read(), content_type='application/json')
            finally:
                source.close()
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code', '')
        status = {'ValidationException': 400, 'AccessDeniedException': 403, 'ResourceNotFoundException': 404,
                  'ThrottlingException': 429, 'ModelTimeoutException': 504}.get(code, 502)
        return error(exc.response.get('Error', {}).get('Message', 'Bedrock request failed'), status)
    except BotoCoreError:
        return error('Bedrock connection or credentials unavailable', 503)

    def events():
        try:
            for event in source:
                if 'chunk' not in event:
                    yield 'event: error\ndata: ' + json.dumps(error('Bedrock stream interrupted', 502)[0]) + '\n\n'
                    return
                data = json.loads(event['chunk']['bytes'])
                yield 'event: ' + data.get('type', 'message') + '\ndata: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
        except (BotoCoreError, ValueError):
            yield 'event: error\ndata: ' + json.dumps(error('Bedrock stream interrupted', 502)[0]) + '\n\n'
        finally:
            source.close()
    response = Response(stream_with_context(events()), content_type='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    response.call_on_close(source.close)
    return response
