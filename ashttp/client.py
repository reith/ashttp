from twisted.internet.protocol import ClientFactory
from twisted.web import http
from twisted.web.http_headers import Headers

import cPickle
from base64 import b64encode, b64decode

from . import tunnel
from .logging import logger
from .policies import KeepAliveParameters

class HTTPRequest(tunnel.Request):
	"""HTTP Request comes from actual proxy or client"""

	def alteredRequest(self):
		"""Obfsucate request"""
		method = self.method
		uri = 'index.php'
		received_headers = self.getAllHeaders().copy()
		headers = {}
		# add additional data to headers
		received_headers['req-uri'] = self.uri
		version = received_headers['req-version'] = self.clientproto
		# pack real headers & additional data
		headers['cookie'] = b64encode(cPickle.dumps(received_headers).encode('zlib'))
		# change headers visible on wire
		headers['host'] = b'luna.ir'
		headers['connection'] = b'keep-alive'

		if self.method == b'CONNECT':
			headers['tunnel'] = 'start'
			method = 'GET'
			self.channel.setTunnelStatus(tunnel.TCPTunnelStatus.BEFORE_NEGOTIATION)
			version = b'HTTP/1.1'
		elif self.method == b'POST':
			headers['content-length'] = received_headers['content-length']

		self.content.seek(0, 0)
		return ((method, uri, version), headers, self.content.read())

	def alterResponse(self):
		"""
		Deobfuscate response
		"""
		old_headers = self.responseHeaders
		if self.channel._tunnelStatus.is_headers_sent_to_server:
			if self.code == 200:
				self.code_message = b'Connection established'
			headers = Headers()
		else:
			pickled_origin_headers = self.responseHeaders.getRawHeaders('set-cookie')
			if pickled_origin_headers is None:
				headers = Headers()
			else:
				headers = cPickle.loads(b64decode(pickled_origin_headers[0]).decode('zlib'))
		self._responseAltered = True
		self.responseHeaders = headers
		return old_headers


class Server(tunnel.HTTPResponder):
	"""
	HTTP tunnel client. obfuscates messages.
	"""
	requestFactory = HTTPRequest
	# use a timeout in not tunnel mode
	_HTTPModeTimeout = 600
	timeOut = _HTTPModeTimeout

	def rawDataReceived(self, data):
		"""
		If this server is acting as TCP Tunnel begining point, It should work in raw mode
		"""
		if self._tunnelStatus._state:
			self.client.writeTCPTunneledData(data)
		else:
			http.HTTPChannel.rawDataReceived(self, data)
	
	def tcpTunnelingStarted(self):
		self.transport.write(b'\r\n')
		self.setRawMode()
		self.client.consumer = self.transport
		self.setTimeout(None)

	def tcpTunnelingFinished(self):
		logger.debug('%s: TCP TUNNELING FINISHED' % self)
		self.setLineMode()
		self._tunnelStatus.no_tunnel()
		self.client._chunkProcessing = False
		self._clientStatus.ended()
		self.setTimeout(self._HTTPModeTimeout)

	def connectionLost(self, reason):
		"""
		Prehandler for connection closing on TCP tunneling.
		"""
		if self._tunnelStatus._state >= tunnel.TCPTunnelStatus.HEADERS_SENT_TO_SERVER:
			# XXX: handle other tunnel states..
			self.client.handleResponseEnd()
			# it finishes request. so it must not call errback on:
			# HTTPChannel.connectionLost > .requests[0].connectionLost > .notifications.errback
		tunnel.HTTPResponder.connectionLost(self, reason)


class Client(tunnel.HTTPRequester):
	"""
	HTTP Client for tunnel client
	"""
	keep_alive = True
	keep_alive_params = KeepAliveParameters(30, 30, 2)

	def writeTCPTunneledData(self, data):
		assert self.server._tunnelStatus.is_established
		self.sendCommand(b'POST', b'/send.php', b'HTTP/1.1')
		self.sendHeader(b'Content-Length', len(data))
		self.endHeaders()
		tunnel.HTTPRequester.writeTCPTunneledData(self, data)

	def handleChunk(self, data):	
		return self.handleResponsePart(data, False)


class HTTPClientFactory(tunnel.HTTPClientFactory):
	protocol = Client


class TunnelClientFactory(tunnel.TunnelFactory):
	protocol = Server


class TunnelClientService(tunnel.TunnelService):
	_clientPool = []
	_useClientPool = True
	_minIdleClients = 2
	_maxTCPConnections = 2

	clientFactory = HTTPClientFactory
	serverFactory = TunnelClientFactory
