from io import BytesIO as StringIO

from twisted.internet.protocol import ClientFactory
from twisted.web import http
from twisted.application import service
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet import defer, error

from .policies import TCPKeepAliveMixin
from .logging import logger


class Status:
	def __getattr__(self, name):
		if name.startswith('is_'):
			return self._state == getattr(self, name[3:].upper())
		if hasattr(self, name.upper()):
			self._state = getattr(self, name.upper())
			self._message = name
			return lambda: None
		raise AttributeError('%s have not attribute %s' % (self.__class__, name))

	def __str__(self):
		return self._message


class ClientStatus(Status):
	ATTACHED = 1
	DETACHED = 2
	ENDED = 3


class OwningClientStatus(Status):
	HAVING = 1
	REQUESTED = 2
	LACKING = 3
	ENDED = 4
	
	def __init__(self):
		self.lacking()


class TCPTunnelStatus(Status):
	NO_TUNNEL = 0
	BEFORE_NEGOTIATION = 1
	HEADERS_SENT_TO_SERVER = 2
	ESTABLISHED = 3
	_message = ''
	_state = 0


class NotProccessingRequest(ValueError):
	pass


class Request(http.Request):
	_responseAltered = False

	def process(self):
		logger.debug('REQUEST FOR %s' % self.channel)
		if self.channel.client is not None:
			self.channel.client.wakeup()
		else:
			self.channel.notifyClientNeed()

	def alteredRequest(self):
		"""
		manipulate request before sending. return (verb, headers, data)
		"""
		raise NotImplemented

	def write(self, data):
		if not self.startedWriting and not self._responseAltered:
			self.alterResponse()
		http.Request.write(self, data)

	def alterResponse(self):
		"""
		manipulate response befor writing.
		MUST set _responseAltered.
		returns old headers.
		"""
		raise NotImplemented

	def setLastModified(self, when):
		raise NotImplemented


class HTTPResponder(http.HTTPChannel, TCPKeepAliveMixin):
	"""
	HTTP Server.
	"""
	requestFactory = None
	client = None

	def __init__(self):
		http.HTTPChannel.__init__(self)
		self._clientStatus = OwningClientStatus()
		self._tunnelStatus = TCPTunnelStatus()
		self._tunnelStatus.no_tunnel()

	def notifyClientNeed(self):
		"""
		Tell service, responder needs requester still.
		"""
		logger.debug('%s: NEEDS CLIENT' % self)
		assert self.client == None
		if self._clientStatus.is_lacking:
			self.factory.acquireClientFor(self)
		else:
			logger.warn('%s: CLIENT NOT ACQUIRED: %s' % (self, self._clientStatus))

	def requestDone(self, req):
		"""
		Request responded.
		"""
		logger.info('%s: (REQUEST DONE=%s)' % (self, req.uri))
		http.HTTPChannel.requestDone(self, req)

		if self._tunnelStatus._state:
			self.tcpTunnelingFinished()

		if self._clientStatus.is_ended:
			self.transport.loseConnection()
		elif self.factory.service._useClientPool and not self.requests:
			self.detachClient("no more request for this")
		elif self.requests:
			logger.debug('%s: MORE_REQUESTS_WAKE_UP' % self)
			self.client.wakeup()

	def detachClient(self, reason=None):
		if self._tunnelStatus._state:
			logger.warn("%s: WARNING: DETACH ON TUNNEL MODE" % self)
			self._tunnelStatus.no_tunnel()
		if self.client is None:
			logger.debug('%s: CLIENT ALREADY DETACHED' % self)
			return
		logger.debug('%s: DETACHING (reason: %s)', self, reason)
		self.client.reason = reason
		client = self.client
		self.client = None
		self._clientStatus.lacking()
		client.detached()
	
	def __str__(self):
		return '[server #%d:(%s)]' % (self.instanceID, self._clientStatus)

	def setTunnelStatus(self, val, dependto=None):
		if dependto is not None:
			if self._tunnelStatus._state != dependto:
				logger.error('%s: !! FSA BUG. expected current state %d but have %d' % \
				(self, dependto, self._tunnelStatus._state))
				raise RuntimeError()
		oldval = self._tunnelStatus._state
		self._tunnelStatus._state = val
		logger.info("%s : TCP STATUS CHANGED %s => %s" % (self, oldval, val))

	def attachClient(self, client):
		"""
		Called when [client] connection to actual other end of tunnel established.
		"""
		assert self.client == None
		logger.debug('%s: ATTACHING' % (self))
		self.client = client
		client.server = self
		self._clientStatus.having()
		client.attached()

	def connectionMade(self):
		logger.info('%s: NEW SERVER' % self)
		if self.keep_alive:
			self.makeSocketKeepAlive()

	def connectionLost(self,  reason):
		logger.info('%s: LOST (reason=%s)' % (self, reason))
		http.HTTPChannel.connectionLost(self, reason)
		client = self.client
		if client is not None:
			# first set client as lacking server. for ease in handling closing
			self.detachClient('received connection closed')
			if not self.factory.service._useClientPool:
				logger.debug("%s: PROPAGATE CLOSE FROM RESPONDER TO %s" % (self, client))
				client._status.ended()
				client.transport.loseConnection()
		del self.factory

	def tcpTunnelingStarted(self):
		"""
		Called when TCP tunnel established
		"""
		raise NotImplemented

	def tcpTunnelingFinished(self):
		"""
		Called when TCP tunneling stopped
		"""
		raise NotImplemented
		

class HTTPRequester(http.HTTPClient, TCPKeepAliveMixin):
	"""
	Simple HTTP Client to handle encoding aware transfer
	"""
	consumer = None
	isReady = True
	_chunkProcessing = False
	server = None
	_comingDataIsNotRaw = False

	def __init__(self):
		self._status = ClientStatus()
		self._status.detached()

	def attached(self):
		""""
		Called when a server picked up this client
		"""
		self._status.attached()
		logger.debug('%s: ATTACHED' % self)
		self.wakeup()

	def wakeup(self):
		"""
		Get next request (if any) from attached server and process it.
		"""
		logger.debug('%s: WAKEUP' % self)
		if self.isReady and self.server.requests:
			assert self.server.client is not None
			if not self.server.requests[0]._disconnected:
				self.getNextRequest()

	def handleChunk(self, data):
		return self.handleResponsePart(data)

	def handleResponsePart(self, data, bufferize=True):
		"""
		This is re-implemented for chunked mode. but used before too.
		It must handle not bufferized mode. For obfuscated TCP response processing.
		"""
		if not bufferize:
			self.consumer.write(data)
		else:
			__buffer = self._HTTPClient__buffer
			__buffer.write(data)
			if __buffer.tell() > 4096:
				self.consumer.write(__buffer.getvalue())
				__buffer.close()
				self._HTTPClient__buffer = StringIO()
				if self.consumer.transport.offset > 0x40000:
					# don't full buffer
					self.pauseProducing()
	
	def rawDataReceived(self, data):
		"""
		reimplementation of rawDataReceived to handle chunked mode responses and
		gotchas discussed in _setComingDataAsNotRaw()
		"""
		if self._comingDataIsNotRaw:
			self._comingDataIsNotRaw = False
			self.setLineMode(data)
		elif self._chunkProcessing:
			self._responseDecoder.dataReceived(data)
		else:
			http.HTTPClient.rawDataReceived(self, data)

	def connectionMade(self):
		logger.info('%s: NEW CLIENT' % self)
		if self.keep_alive:
			self.makeSocketKeepAlive()

	def sendCommand(self, command, path, version):
		logger.info("%s: >>> %s %s %s" % (self, command, path, version))
		self.transport.writeSequence([command, b' ', path, b' ', version, b'\r\n'])

	def __str__(self):
		if self.server is not None:
			return '[client #%d <- %s]' % (self.instanceID, self.server)
		else:
			return '[client #%d (%s)]' % (self.instanceID, self._status)

	def __getattr__(self, name):
		if name == 'request':
			if not self.isReady:
				return self.server.requests[0]
			else:
				raise NotProccessingRequest('no current request [server=%s]' % self.server)
		raise AttributeError('{0} instance has no attibute {1}'.format(self.__class__, name))

	def handleRequestError(self, failure):
		logger.info('%s: E_REQUEST: %s' % (self, failure))
		failure.trap(error.ConnectionDone)
		self._status.ended()
		self.transport.loseConnection()

	def getNextRequest(self):
		"""
		Send preprocessed request to server/proxy/tunnel
		"""
		logger.debug('%s: PROCESSING NEXT', self)
		assert self.isReady == True
		self.isReady = False
		req_line, headers, data = self.request.alteredRequest()
		self.consumer = self.server.requests[0]
		self.request.notifyFinish().addCallbacks(lambda _: self._reset(), self.handleRequestError)

		if req_line is not None:
			self.sendCommand(*req_line)
			logger.info("%s: requesting >>> [%s]" % (self, self.request.uri))

		if headers is not None:
			for key, val in headers.items():
				self.sendHeader(key, val)
			self.endHeaders()
		self.transport.write(data)
		self.request.registerProducer(self, True)
		if self.server._tunnelStatus._state:
			self.server.setTunnelStatus(TCPTunnelStatus.HEADERS_SENT_TO_SERVER, TCPTunnelStatus.BEFORE_NEGOTIATION)
			assert data == '', 'BAD TCP TUNNEL IMPLEMENTATION' + data

	def _setStatusWaitingMode(self):
		"""
		set requester attributes to processing continue at status line step.
		"""
		if not self.line_mode:
			self.setLineMode()
		self.firstLine = True
		self.length = None

	def _cleanBuffer(self):
		if self._HTTPClient__buffer is not None:
			self._HTTPClient__buffer.close()
			self._HTTPClient__buffer = None

	def _reset(self):
		"""
		Clear out attributes, making ready to process new request on same tcp.
		must not depend to server and request.
		"""
		logger.debug('%s: RESET' % self)
		if self._chunkProcessing:
			# should be reduced in favour of tcpTunnelingFinished
			logger.debug('%s: CHUNKMODE IN RESET' % self)
			self._chunkProcessing = False
		self._cleanBuffer()
		self.isReady = True
		self._setStatusWaitingMode()

	def handleResponseEnd(self):
		logger.debug('%s: RESPONSE END' % self)
		http.HTTPClient.handleResponseEnd(self)
		self.consumer = None
		self.request.unregisterProducer()
		self.request.finish()

	def handleResponse(self, resp):
		self.consumer.write(resp)

	def handleHeader(self, key, val):
		"""
		Handle header comes from tunnel/proxy
		"""
		self.request.responseHeaders.addRawHeader(key, val)
		if key.lower() == b'transfer-encoding' and val.lower() == b'chunked':
			logger.debug('%s: CHUNKED -> On' % self)
			self._responseDecoder = http._ChunkedTransferDecoder(self.handleChunk, self.chunkResponseEnded)
			self._chunkProcessing = True

	def chunkResponseEnded(self, rest):
		"""
		Clear chunk attributes and notify response end
		"""
		self._responseDecoder = None
		self._chunkProcessing = False
		logger.debug('%s: CHUNKED -> Off' % self)
		if rest:
			logger.error('%s: REST_DATA_AFTER_CHUNK (len=%s)' % (self, len(rest)))
		self.handleResponseEnd()

	def handleEndHeaders(self):
		if self.request.code == 100:
			self._setComingDataAsNotRaw()
			self._setStatusWaitingMode()
			# FIXME: headers suppressed
			self.request.responseHeaders._rawHeaders.clear()
			return

		self.request.alterResponse()
		self.request.write('')
		if self.request.method == b'HEAD' or self.request.code in http.NO_BODY_CODES or self.length == 0:
			self._setComingDataAsNotRaw()
			self.handleResponseEnd()

		elif self.server._tunnelStatus._state:
			self.server.setTunnelStatus(TCPTunnelStatus.ESTABLISHED, TCPTunnelStatus.HEADERS_SENT_TO_SERVER)
			self.server.tcpTunnelingStarted()
	
	def _setComingDataAsNotRaw(self):
		"""
		http.HTTPClient has some gotchas when it comes to proccessing sequences of
		some responses causing headers for next response being marked as body for
		current response. That's because setRawMode() called after handleEndHeaders
		in HTTPClient's FSA. So use this for such responses, since setLineMode() in
		handleEndHeaders couldn't help.
		The only user of this will be reimplemented rawDataReceived that takes care
		of this.
		"""
		self._comingDataIsNotRaw = True

	def handleStatus(self, version, status, message):
		try:
			self.request.setResponseCode(int(status), message)
		except ValueError:
			logger.error('%s: HANDLE_RESPONSE_ERROR: %s, %s, %s, %s' % (self, version, status, message, self.__dict__))
			raise

	def writeTCPTunneledData(self, data):
		self.transport.write(data)

	def connectionLost(self, reason):
		logger.info('%s: LOST (reason=%s)' % (self, reason))
		# ------->8----- debug. just report
		if not self.factory.service._useClientPool:
			if self.isReady:
				logger.debug('%s: W_CLOSE_1: ENDED CLIENT DIED IN NOT POOLED SERVICE' % self)
			else:
				logger.debug('%s: W_CLOSE_2: NOT ENDED CLIENT DIED IN NOT POOLED SERVICE' % self)
		else:
			if not self.isReady:
				logger.debug('%s: W_CLOSE_3 NOT ENDED CLIENT DIED IN POOLED SERVICE' % self)
		# ------>8---------
		if self.server is not None and not self._status.is_ended:
			server = self.server
			server_should_die = not self.factory.service._useClientPool
			if not self.isReady:
				# relay received data of not ended response. hopefully client could handle it.
				# it happens when response is not standard. e.g responding gziped body without
				# resetting content-length, py2exe.org case. so we must close tcp, regardless
				# to connection pool (this will done by setting client status as ended), then
				# client can find out response is over.
				server._clientStatus.ended()
				self.handleResponseEnd()
			else:
				server.detachClient('client lost itself')
				if server_should_die:
					server.transport.loseConnection()
		self._cleanup(reason)

	def _cleanup(self, close_reason):
		"""
		Cleanup after lost connection
		"""
		self.factory.clientDead(self, close_reason)
		del self.factory
		self.consumer = None
		self._cleanBuffer()

	def detached(self):
		"""
		Server released this client.
		"""
		server = self.server
		self.server = None
		self._status.detached()
		if self.paused and not self.transport.disconnecting:
			self.transport.loseConnection()
		if not self.isReady:
			logger.warn('%s: DETACHED A NOT YET READY CLIENT. SEE WHETHER IT WILL REUSED' % self)
		logger.debug('%s: DETACHED' % self)


class HTTPClientFactory(ClientFactory):
	protocol = None
	last_tcp_id = 0

	def buildProtocol(self, addr):
		p = self.protocol()
		p.instanceID = self.last_tcp_id
		HTTPClientFactory.last_tcp_id += 1
		p.factory = self
		return p

	def clientDead(self, client, reason):
		if self.service._useClientPool:
			self.service._clientPool.remove(client)
			self.service.makePoolFull()


class TunnelFactory(http.HTTPFactory):
	protocol = None
	last_tcp_id = 0

	def buildProtocol(self, addr):
		p = self.protocol()
		p.instanceID = self.last_tcp_id
		p.factory = self

		TunnelFactory.last_tcp_id += 1
		return p

	def acquireClientFor(self, server):
		"""
		Get a client (pick an idle or make an new) for server
		"""
		server._clientStatus.requested()
		d = defer.maybeDeferred(self.service.pickClient)
		def errback(error):
			logger.error('Error on acquiring client: %s error' % error)
			raise RuntimeError('Can\'t acquire client.')
		d.addErrback(errback)
		d.addCallbacks(self._attachClientIfServerAlive, lambda _: server.transport.loseConnection(), callbackArgs=(server,))

	def _attachClientIfServerAlive(self, client, server):
		if server.transport.connected:
			server.attachClient(client)
		else:
			logger.debug('%s: SERVER DIED BEFORE MAKING CLIENT' % server)
			if not self.service._useClientPool:
				client.transport.loseConnection()
			# else: remain on pool


class TunnelService(service.Service):
	_clientPool = []
	_connectorPool = []
	_useClientPool = None # True or False
	_minIdleClients = None

	clientFactory = HTTPClientFactory
	serverFactory = TunnelFactory

	remote_host = None
	remote_port = None

	def __init__(self, reactor):
		self._reactor = reactor
		self.serverFactory.service = self.clientFactory.service = self
		if self._useClientPool:
			self.makePoolFull()

	def _pickIdleClient(self):
		"""
		Return idle (detached and ready client) from pool or None
		"""
		logger.info('POOL LENGTH IS: %s' % len(self._clientPool))
		for client in self._clientPool:
			if client.isReady != client._status.is_detached:
				logger.warn('%s: E_CLIENT_POOL_1 isReady: %s _status: %s' % (client, client.isReady, client._status))
			if client.isReady and client._status.is_detached:
				logger.info('PICKED CLIENT FROM POOL')
				return client
		return None

	def pickClient(self):
		"""
		Pick a idle client from pool or make new client.
		"""
		client = self._pickIdleClient() if self._useClientPool else None
		return client if client is not None else self._newClient()

	def makePoolFull(self):
		"""
		Make idle clients (if needed) and put it in pool.
		"""
		logger.info('MAKING POOL FULL')
		rc = self._minIdleClients
		rc -= len(filter(lambda c: c._status.is_detached, self._clientPool))
		for _ in xrange(rc):
			self._newClient()

	def getServerFactory(self):
		f = self.serverFactory()
		return f

	def _newClient(self):
		"""
		Make new HTTPClient. return connector deferred.
		"""
		remote_ep = TCP4ClientEndpoint(self._reactor, self.remote_host, self.remote_port)
		connector = remote_ep.connect(self.clientFactory())
		if self._useClientPool:
			def _add_to_pool(client):
				logger.debug('MADE NEW CLIENT %s', str(client))
				self._clientPool.append(client)
				return client
			connector.addCallback(_add_to_pool)
		return connector
