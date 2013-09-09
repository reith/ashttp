from twisted.internet.protocol import ClientFactory
from twisted.web import http
from twisted.web.http_headers import Headers

import cPickle
from base64 import b64encode, b64decode

from ashttp import tunnel
from ashttp.logging import logger
from ashttp.policies import KeepAliveParameters

class ObfuscatedRequest(tunnel.Request):
	"""
	Obfuscated Request sent from client end of tunnel
	"""
	def process(self):
		if self.channel.tunnelStatus() == tunnel.TCPTunnelStatus.ESTABLISHED:
			if self.method == b'POST':
				del self.channel.requests[1] # suicide
				self.content.seek(0,0)
				self.channel.client.transport.write(self.content.read())
				self.__cleanup()
			else:
				# while this is possible, tcp handling has problem and above if is not safe neither
				logger.error('%s: NON_TCP_DATA_IN_TUNNEL_MODE: mode: %s, req: %r' % (self.channel.responderID(), self.channel.tunnelStatus(), self))
				self.channel._tunnelStatus = 0
				tunnel.Request.process(self)
		else:
			tunnel.Request.process(self)

	def __cleanup(self):
		"""
		Cleanup fake request
		"""
		del self.channel
		self.content.close()
		del self.content

	def alteredRequest(self):
		assert not self.channel.tunnelStatus()
		received_headers = self.getAllHeaders().copy()
		method = self.method
		headers = cPickle.loads(b64decode(received_headers['cookie']).decode('zlib'))
		uri = headers.pop('req-uri')
		version = headers.pop('req-version')
		self.content.seek(0, 0)
		data = self.content.read()
		if self.method == b'GET' and received_headers.get('tunnel', False):
			method = b'CONNECT'
			self.channel.setTunnelStatus(tunnel.TCPTunnelStatus.BEFORE_NEGOTIATION)
		return ((method, uri, version), headers, data)

	def alterResponse(self):
		if self.startedWriting:
			raise RuntimeError
		old_headers = self.responseHeaders
		headers = Headers()
		if self.channel.tunnelStatus():
			headers.addRawHeader(b'content-type', b'gzip')
			if self.code == 200: # hide connection established
				self.code_message = http.RESPONSES[http.OK]
		else:
			headers.addRawHeader(b'set-cookie', b64encode(cPickle.dumps(self.responseHeaders).encode('zlib')))
			for must_kept_key in [b'content-length']:
				val = self.responseHeaders.getRawHeaders(must_kept_key)
				if val is not None:
					headers.addRawHeader(must_kept_key, val[0])
		headers.addRawHeader(b'connection', b'keep-alive')
		self.responseHeaders = headers
		self._responseAltered = True
		return old_headers


class Server(tunnel.HTTPResponder):
	"""
	HTTP tunnel server. deobfuscates obfuscated messages.
	"""
	requestFactory = ObfuscatedRequest
	keep_alive = True
	keep_alive_params = KeepAliveParameters(300, 60, 2)

	def tcpTunnelingStarted(self):
		self.client.setRawMode()

	def tcpTunnelingFinished(self):
		logger.debug('%s: TCP TUNNELING FINISHED' % self.responderID())
		self._clientStatus = tunnel.OwningClientStatus.ENDED
		self._tunnelStatus = 0


class Client(tunnel.HTTPRequester):

	def rawDataReceived(self, data):
		"""
		Called when acting as TCP tunnel endpoint to actual proxy/server.
		"""
		if self.isReady:
			logger.error('%s: DATA RECEIVED WHILE NOT WANTED' % self.requesterID())
			return

		if self.server.tunnelStatus() == tunnel.TCPTunnelStatus.ESTABLISHED:
			self.consumer.write(data)
			data_type = 'TCP'
		else:
			tunnel.HTTPRequester.rawDataReceived(self, data)
			data_type = 'UNKNOWN_RAW'
		logger.debug('%s: WROTE %s (len=%d)' % (self.requesterID(), data_type, len(data)))

	def connectionLost(self, reason):
		if self.server is not None and self.server.tunnelStatus():
			self.handleResponseEnd() # server'll be closed in requestDone
			self._cleanup(reason)
		else:
			tunnel.HTTPRequester.connectionLost(self, reason)


class HTTPClientFactory(tunnel.HTTPClientFactory):
	protocol = Client


class TunnelServerFactory(tunnel.TunnelFactory):
	protocol = Server


class TunnelServerService(tunnel.TunnelService):
	_clientPool = []
	_useClientPool = False
	_minIdleClients = None

	clientFactory = HTTPClientFactory
	serverFactory = TunnelServerFactory
