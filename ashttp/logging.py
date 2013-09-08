from __future__ import absolute_import

__all__ = ['logger']

class Logger:

	def useTwistdLogger(self):
		from twisted.python.log import logging
		self._logger = logging.getLogger()

	def usePythonStdLogger(self, severity='info', filename='ashttp.log'):
		import logging
		logger = logging.getLogger()
		logger.setLevel(getattr(logging, severity.upper()))
		logger.addHandler(logging.FileHandler(filename))
		self._logger = logger

	def __getattr__(self, name):
		return getattr(self._logger, name)

logger = Logger()
