import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock,patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from network import PublicTransport,resolve_public


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_dns_results_rejected(self):
        loop=asyncio.get_running_loop()
        for address in ['127.0.0.1','10.0.0.1','169.254.169.254','::1','::ffff:127.0.0.1']:
            with patch.object(loop,'getaddrinfo',AsyncMock(return_value=[(0,0,0,'',(address,80))])):
                with self.assertRaises(PermissionError):await resolve_public('test.example',80)

    async def test_pin_checked_ip_preserves_host_and_tls_name(self):
        received=[]
        async def handler(request):
            received.append(request)
            return httpx.Response(200,text='ok')
        transport=PublicTransport(httpx.MockTransport(handler),resolver=AsyncMock(return_value='93.184.216.34'))
        async with httpx.AsyncClient(transport=transport) as client:
            response=await client.get('https://example.com/docs')
        self.assertEqual(response.text,'ok')
        self.assertEqual(received[0].url.host,'93.184.216.34')
        self.assertEqual(received[0].headers['host'],'example.com')
        self.assertEqual(received[0].extensions['sni_hostname'],'example.com')

    async def test_redirect_is_checked_before_connecting(self):
        received=[]
        async def handler(request):
            received.append(request)
            return httpx.Response(302,headers={'Location':'http://127.0.0.1/admin'})
        resolver=AsyncMock(side_effect=['93.184.216.34','127.0.0.1'])
        async with httpx.AsyncClient(transport=PublicTransport(httpx.MockTransport(handler),resolver),follow_redirects=True) as client:
            with self.assertRaises(PermissionError):await client.get('https://example.com/')
        self.assertEqual(len(received),1)

    async def test_credentials_and_non_web_ports_rejected(self):
        for url in ['http://user:pw@example.com/','http://example.com:8090/']:
            async with httpx.AsyncClient(transport=PublicTransport(httpx.MockTransport(lambda r:httpx.Response(200)))) as client:
                with self.assertRaises(PermissionError):await client.get(url)
