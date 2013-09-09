import socket
from collections import namedtuple

def KeepAliveParameters(idle=30, interval=30, count=2):
	return namedtuple('KeepAliveParams', 'idle interval count')(idle, interval, count)


class TCPKeepAliveMixin:
	keep_alive = False

	def makeSocketKeepAlive(self):
		self.transport.setTcpKeepAlive(True)
		if self._keepAliveIsConfigurable():
			self._setKeepAliveOptions()

	def _keepAliveIsConfigurable(self):
		try:
			socket.TCP_KEEPCNT , socket.TCP_KEEPIDLE , socket.TCP_KEEPINTVL
			return True
		except AttributeError:
			return False

	def _setKeepAliveOptions(self):
		if getattr(self, 'keep_alive_params', True):
			self.keep_alive_params = KeepAliveParameters()
		_socket = self.transport.socket
		_socket.setsockopt(socket.SOL_TCP, socket.TCP_KEEPIDLE, self.keep_alive_params.idle)
		_socket.setsockopt(socket.SOL_TCP, socket.TCP_KEEPINTVL, self.keep_alive_params.interval)
		_socket.setsockopt(socket.SOL_TCP, socket.TCP_KEEPCNT, self.keep_alive_params.count)
