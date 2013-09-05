import sys
from twisted.application import service

def run_with_twistd(options, application_name=''):
	from twisted.scripts.twistd import run
	opts = ['-l', '/dev/null']
	if options:
		opts.extend(options)
	sys.argv[1:] = opts

	application = service.Application(application_name)
	service.loadApplication = lambda f, k, p=None: application
	run()
