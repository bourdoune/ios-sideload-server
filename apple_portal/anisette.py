"""Use the existing anisette service without changing its provisioned identity."""
import os
import requests

CLIENT_INFO = '<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.akd/1.0)>'


def get_headers():
    endpoint = os.environ.get('ALTSERVER_ANISETTE_SERVER', 'http://127.0.0.1:6970')
    response = requests.get(endpoint, timeout=15)
    response.raise_for_status()
    headers = response.json()
    required = ('X-Apple-I-MD', 'X-Apple-I-MD-M', 'X-Apple-I-MD-LU', 'X-Mme-Device-Id')
    if not all(isinstance(headers.get(key), str) and headers[key] for key in required):
        raise ValueError('Anisette service returned incomplete identity headers')
    headers = {key: str(value).strip() for key, value in headers.items()}
    headers['X-MMe-Client-Info'] = CLIENT_INFO
    headers['X-Apple-Locale'] = headers.get('X-Apple-Locale', 'en_US').replace('-', '_')
    return headers
