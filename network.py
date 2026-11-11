"""Resolve public hosts and pin each HTTP connection to the checked address."""
import asyncio
import ipaddress
import socket
import httpx


async def resolve_public(host, port):
    answers=await asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM)
    addresses=list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise PermissionError('该地址不属于可访问的公网地址')
    return sorted(addresses,key=lambda address:ipaddress.ip_address(address).version)[0]


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner=None, resolver=resolve_public):
        self.inner=inner or httpx.AsyncHTTPTransport(trust_env=False,limits=httpx.Limits(max_keepalive_connections=0))
        self.resolver=resolver

    async def handle_async_request(self, request):
        url=request.url
        if url.scheme not in ('http','https') or url.username or url.password:
            raise PermissionError('只支持不含登录凭证的 HTTP 或 HTTPS 地址')
        port=url.port or (443 if url.scheme=='https' else 80)
        if port not in (80,443):
            raise PermissionError('仅支持标准网页端口')
        address=await self.resolver(url.host,port)
        if not ipaddress.ip_address(address).is_global:
            raise PermissionError('该地址不属于可访问的公网地址')
        headers=request.headers.copy()
        headers['host']=url.netloc.decode('ascii')
        pinned=httpx.Request(request.method,url.copy_with(host=address),headers=headers,
            stream=request.stream,extensions={**request.extensions,'sni_hostname':url.host})
        return await self.inner.handle_async_request(pinned)

    async def aclose(self):
        await self.inner.aclose()
