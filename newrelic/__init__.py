import os
import newrelic.agent
from . import controllers
from odoo.tools import config
from odoo import http, service


import logging
_logger = logging.getLogger(__name__)


def initialize_agent():
    """Try to start the agent with a variety of configurations."""
    try:
        newrelic.agent.initialize(config['new_relic_config_file'], config['new_relic_environment'])
    except KeyError:
        try:
            newrelic.agent.initialize(config['new_relic_config_file'])
        except KeyError:
            _logger.info('NewRelic setting up from env variables')
            newrelic.agent.initialize()


def patch_wsgi():
    """Patch the Odoo WSGI handler."""
    try:
        instrumented = http.Application._nr_instrumented
    except AttributeError:
        instrumented = False
    if instrumented:
        _logger.info("NewRelic instrumented already")
        return False
    try:
        http.Application.__call__ = newrelic.agent.WSGIApplicationWrapper(http.Application.__call__)
    except AttributeError as exc:
        # If sentry (from OCA/server-tools) was first, this error occurs:
        # AttributeError: 'SentryWsgiMiddleware' object attribute '__call__' is read-only
        # Fallback on patching the class and the instanciated server, but for threaded instances
        # (such as Odoo.sh) this will not be sufficient.
        http.Application = newrelic.agent.WSGIApplicationWrapper(http.Application)
        server = service.server.server
        if server:
            try:
                instrumented = server.app._nr_instrumented
            except AttributeError:
                instrumented = False
            if not instrumented:
                server.app = newrelic.agent.WSGIApplicationWrapper(server.app)
                server.app._nr_instrumented = True
        if not config["workers"]:
            _logger.warning(exc)
            _logger.warning(
                "Failed to patch the WSGI application. Try loading newrelic as a server wide module."
            )
    http.Application._nr_instrumented = True
    return True


def patch_bus_controller():
    """Patch the bus controller separately."""
    try:
        _logger.info('attaching to bus controller')
        import odoo.addons.bus.controllers.main
        newrelic.agent.wrap_background_task(odoo.addons.bus.websocket, 'Websocket._dispatch_bus_notifications')
        _logger.info('finished attaching to bus controller')
    except Exception as e:
        _logger.exception(e)


def patch_methods():
    """Additional configurable hooks.

    Can be comma separated like
    odoo.models.BaseModel:public,odoo.other.Something:limited
    """
    nr_odoo_trace = os.environ.get('NEW_RELIC_ODOO_TRACE', config.get('new_relic_odoo_trace'))
    # will default to a limited set
    if nr_odoo_trace is None:
        # it is None because it got a default, lets provide one
        nr_patches = ['odoo.models.BaseModel:limited']
    else:
        # the user specified, so they may intend for it to be unset
        nr_patches = nr_odoo_trace.strip().split(',')
    try:
        _logger.info('Applying Tracing to %s' % (nr_patches))
        for patch in nr_patches:
            patch_base, patch_type = patch.split(':')
            _module = None
            _paths = []
            if patch_base == 'odoo.models.BaseModel':
                import odoo.models
                _module = odoo.models
                if patch_type == 'all':
                    _paths += ['BaseModel.%s' % (func, ) for func in dir(odoo.models.BaseModel) if callable(getattr(odoo.models.BaseModel, func)) and not func.startswith("__")]
                elif patch_type == 'public':
                    _paths += ['BaseModel.%s' % (func, ) for func in dir(odoo.models.BaseModel) if callable(getattr(odoo.models.BaseModel, func)) and not func.startswith("_")]
                elif patch_type == 'limited':
                    _paths += [
                        # CRUD
                        'BaseModel.create',
                        'BaseModel.read',
                        'BaseModel.read_group',
                        'BaseModel.write',
                        'BaseModel.unlink',
                        # Search
                        'BaseModel.search',
                        'BaseModel.search_read',
                        'BaseModel.search_count',
                    ]
            if _module:
                for path in _paths:
                    newrelic.agent.wrap_function_trace(_module, path)
    except Exception as e:
        _logger.exception(e)


def setup_error_handling():
    """Patch error handling of the Odoo request dispatchers."""
    def status_code(exc, value, tb):
        from werkzeug.exceptions import HTTPException

        # Werkzeug HTTPException can be raised internally by Odoo or in
        # user code if they mix Odoo with Werkzeug. Filter based on the
        # HTTP status code.

        if isinstance(value, HTTPException):
            return value.code

    def _nr_wrapper_handle_error(wrapped):
        def handle_error(*args, **kwargs):
            transaction = newrelic.agent.current_transaction()

            if transaction is None:
                return wrapped(*args, **kwargs)

            transaction.notice_error(status_code=status_code)

            name = newrelic.agent.callable_name(args[1])
            with newrelic.agent.FunctionTrace(transaction, name):
                return wrapped(*args, **kwargs)

        return handle_error

    for target in (http.HttpDispatcher, http.JsonRPCDispatcher):
        target.handle_error = _nr_wrapper_handle_error(target.handle_error)


def post_load():
    if config.get("stop_after_init"):
        # Only patch servers that will actually serve
        return
    initialize_agent()
    if patch_wsgi():
        # Only execute these when the server is patched successfully
        _logger.info("WSGI patching done")
        patch_bus_controller()
        patch_methods()
        setup_error_handling()
