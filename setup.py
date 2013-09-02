from setuptools import setup
from ashttp import __version__

setup(
	name='ashttp',
	version=__version__,
	description='HTTP Tunnel implementation with Twisted',
	url='https://github.com/reith/ashttp', 

	packages=['ashttp'],
	scripts=['ashttps', 'ashttpc'],

	install_requires=['Twisted>=13', 'Twisted-Web>=13'],
)
