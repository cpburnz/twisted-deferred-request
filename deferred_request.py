# coding: utf-8
"""
Twisted Deferred Request
========================

Introduction
------------

This module contains an implementation of a Twisted Request that allows
Resources rendering to return Deferreds instead of ``NOT_DONT_YET``. By
using deferreds one can ensure that a request will always be finished
regardless of any errors while rendering, and allows for the convenient
use of ``inlineCallbacks``.


Source
------

The source code for Twisted Deferred Request is available from the
GitHub repo `cpburnz/twisted-deferred-request`_.

.. _`cpburnz/twisted-deferred-request`: https://github.com/cpburnz/twisted-deferred-request.git


Use
---

Here's an example using the deferred request::

    from twisted.internet import defer, reactor
    from twisted.web import resource, server
    
    from deferred_request import DeferredRequest
    
    class DeferredResource(resource.Resource):
    
        @defer.inlineCallbacks
        def render(self, request):
            data = yield deferredDatabaseRequest()
            request.write(data)
            request.finish() # This is optional
    
    class Site(server.Site):        
        requestFactory = DeferredRequest
        displayTracebacks = True
    
    def main():
        resrc = DeferredResource()
        site = server.Site(resrc)
        reactor.listenTCP(8080, site)
        reactor.run()
        return 0
    
    if __name__ == '__main__':
        exit(main())
"""

__author__ = "Caleb P. Burns <cpburnz@gmail.com>"
__copyright__ = "Copyright (C) 2012 by Caleb P. Burns"
__license__ = "MIT"
__version__ = "0.8.0"
__status__ = "Development"

import codecs
import datetime
import os
import time
import urllib
try:
	import cStringIO as StringIO
except ImportError:
	import StringIO
	
from dateutil import parser as dt_parser, tz
from twisted.internet import defer
from twisted.python import failure, log, urlpath
from twisted.web import http, server, util as webutil

__all__ = ['DeferredRequest', 'RequestException', 'RequestDisconnected', 'ResponseStarted']

_utc = tz.tzutc()

# Static HTML responses.
_server_error_html = u"""
<!DOCTYPE html>
<html>
	<head><title>500 Internal Server Error</title></head>
	<body>500: There was an internal server error processing your request for <em>{path}</em></body>
</html>
""".strip()

_server_traceback_html = u"""
<!DOCTYPE html>
<html>
	<head><title>500 Internal Server Error</title></head>
	<body>
		<h1>Server Traceback for</h1>
		<h2>{path}</h2>
		<div>{traceback}</div>
	</body>
</html>
"""

def datetime_to_rfc2822(dt):
	"""
	Formats a datetime as a RFC 2822 string.
	
	*dt* (``datetime`` or ``float``) is the datetime. If a ``float``,
	this will be considered the seconds passed since the UNIX epoch
	relative to UTC. If a **naive** ``datetime``, this will also be
	considered relative to UTC.
	
	Returns the formatted datetime (``str``).
	"""
	if isinstance(dt, datetime.datetime):
		tm = dt.utctimetuple()
	elif isinstance(dt, (float, int, long)):
		tm = time.gmtime(dt)
	else:
		raise TypeError("dt:%r is not a datetime or float." % dt)
	return time.strftime('%a, %d %b %Y %H:%M:%S GMT', tm)


class RequestException(Exception):
	"""
	The ``RequestException`` class is the base class that all request
	exceptions will inherit from.
	"""


class RequestDisconnected(RequestException):
	"""
	The ``ResponseStarted`` error is raised when an action is performed on
	a request that cannot be performed after the response has been
	disconnected (e.g., after writing the response).
	"""


class ResponseStarted(RequestException):
	"""
	The ``ResponseStarted`` error is raised when an action is performed on
	a request that cannot be performed after the response has started
	(e.g., setting headers and cookies).
	"""
	

class DeferredRequest(http.Request):
	"""
	The ``DeferredRequest`` class represents a Twisted HTTP request. This
	class implementes the ``twisted.web.iweb.IRequest`` interface. It can
	be used like ``twisted.web.server.Request`` as a drop-in replacement
	(except for undocumented attributes).
	
	This implementation allows ``twisted.internet.defer.Deferred``s to be
	returned when rendering ``twisted.web.resource.Resource``s.
	
	.. TODO: Test to see if this is compatible with being queued.
	"""
	
	def __init__(self, channel, queued):
		"""
		Initializes a ``DeferredRequest`` instance.
		
		*channel* (``twisted.web.http.HTTPChannel``) is the channel we are
		connected to.
		
		*queued* (``bool``) is whether we are in the request queue
		(``True``), or if we can start writing to the transport (``False``).
		"""
		
		self.args = None
		"""
		*args* (``dict``) is a mapping that maps decoded query argument and
		POST argument name (``str``) to a ``list`` of its values (``str``).
		
		.. NOTE: Inherited from ``twisted.web.http.Request``.
		"""
		
		self._disconnected = False
		"""
		*_disconnected* (``bool``) indicates whether the client connection
		is disconnected (``True``), or still connected (``False``).
		
		.. NOTE: Inherited from ``twisted.web.http.Request``.
		"""
		
		self.finished = False
		"""
		*finished* (``bool``) is whether the request is finished (``True``),
		or not (``False``).
		
		.. NOTE: Inherited from ``twisted.web.http.Request``.
		"""
		
		self.method = http.Request.method
		"""
		*method* (``str``) is the HTTP method that was used (e.g., "GET" or
		"POST").
		
		.. NOTE: Inherited from ``twisted.web.http.Request``.
		"""
		
		self.notifications = None
		"""
		*notifications* (``list``) contains the list of
		``twisted.internet.defer.Deferred``s to be called once this request
		is finished. 
		
		.. NOTE: Inherited from ``twisted.web.http.Request``.
		"""
		
		self.path = None
		"""
		*path* (``str``) is the path component of the URL (no query
		arguments).
		
		.. NOTE: Inherited from ``twisted.web.http.Request``.
		"""
		
		self.prepath = None
		"""
		*prepath* (``list``) contains the path components traversed up to
		the current resource.
		"""
		
		self.postpath = None
		"""
		*postpath* (``list``) contains the path components remaining at the
		current resource.
		"""
		
		self.requestHeaders = None
		"""
		*requestHeaders* (``twisted.web.http.Headers``) contains all
		received request headers.
		
		.. NOTE: Inherited from ``twisted.web.http.Request``.
		"""
		
		self.responseHeaders = None
		"""
		*responseHeaders* (``twisted.web.http.Headers``) contains all
		response headers to be sent.
		
		.. NOTE: Inherited from ``twisted.web.http.Request``.
		"""
		
		self._resp_buffer = None
		"""
		*_resp_buffer* (``StringIO.StringIO``) is the response buffer when
		*_resp_buffered* is ``True``.
		"""
		
		self._resp_buffered = True
		"""
		*_resp_buffered* (``bool``) is whether the response is buffered
		(``True``), or unbuffered (``False``). Default is ``True`` for
		``buffered``.
		"""
		
		self._resp_chunked = None
		"""
		*_resp_chunked* (``bool``) indicates whether the response will be
		chunked (``True``), or not (``False``).
		"""
		
		self._resp_code = http.OK
		"""
		*_resp_code* (``int``) is the HTTP response status code. The default
		code is 200 for "OK".
		"""
		
		self._resp_code_message = http.RESPONSES[http.OK]
		"""
		*_resp_code_message* (``str``) is the HTTP response status code
		message. The default message is "OK".
		"""
		
		self._resp_content_type = 'text/html'
		"""
		*_resp_content_type* (``str``) is the response content type. Default
		is "text/html".
		"""
		
		self._resp_cookies = []
		"""
		*_resp_cookies* (``list``) contains the list of response cookies. 
		"""
		
		self._resp_enc = None
		"""
		*_resp_enc* (``str``) is the response encoding. Default is ``None``.
		"""
		
		self._resp_error_cb = None
		"""
		*_resp_error_cb* (**callable**) is the callback function for when
		there is an error rendering a request.
		"""
		
		self._resp_error_cb_args = None
		"""
		*_resp_error_cb_args* (**sequence**) contains any positional
		arguments to pass to *_resp_error_cb*.
		"""
		
		self._resp_error_cb_kw = None
		"""
		*_resp_error_cb_kw* (``dict``) contains any keyword arguments to
		pass to *_resp_error_cb*.
		"""
		
		self._resp_last_modified = None
		"""
		*_resp_last_modified* (``datetime``) is when the resource was last
		modified.
		"""
		
		self._resp_nobody = None
		"""
		*_resp_nobody* (``bool``) indicates whether there should be a
		message body in the response based upon *code*.
		"""
		
		self._resp_started = False
		"""
		*_resp_started* (``bool``) indicates whether the response has been
		started (``True``), or not (``False``).
		"""
		
		self._root_url = None
		"""
		*_root_url* (``str``) is the URL remembered from a call to
		*rememberRootURL()*.
		"""
		
		self.sentLength = 0
		"""
		*sentLength* (``int``) is the total number of bytes sent as part of
		response body.
		
		.. NOTE: Inherited from ``twisted.web.http.Request``.
		"""
		
		self.site = None
		"""
		*site* (``twisted.internet.server.Site``) is the site that created
		the request.
		"""
		
		self.sitepath = None
		"""
		*sitepath* (``list``) contains the path components (``str``)
		traversed up to the site. This is used to determine cookie names
		between distributed servers and dosconnected sites.
		
		.. NOTE: Set by ``twisted.web.server.Site.getResourceFor()``.
		"""
		
		self.uri = http.Request.uri
		"""
		*uri* (``str``) is the full URI that was requested (including query
		arguments).
		
		.. NOTE: Inherited from ``twisted.web.http.Request``.
		"""
		
		http.Request.__init__(self, channel, queued)
		
	@staticmethod
	def buildPath(path):
		"""
		The the specified path.
		"""
		return '/' + '/'.join(map(urllib.quote, path))
		
	def buildURL(self, path, host=None, port=None, secure=None, query=None):
		"""
		Builds the URL for the specified path.
		
		*path* (``str`` or **sequence**) is the path. If **sequence**,
		contains the path segments (``str``).
		
		*host* (``str``) is the host name. Default is ``None`` to use the
		host from the request.
		
		*port* (``int``) is the port number. Default is ``None`` to use the
		port from the request.
		
		*secure* (``bool``) is whether the URL should be "https" (``True``),
		or "http" (``False``). Default is ``None`` to use the whether the
		request connection was secure or not.
		
		*query* (``dict`` or **sequence**) contains the query arguments. If
		a ``dict``, maps argument *key* to *value*. If a **sequence**,
		contains 2-``tuple``s of *key* and *value* pairs. *key* is a
		``str``, and *value* can be either a single value (``str``) or a
		**sequence** of values (``str``).
		
		Returns the built URL (``str``).
		"""
		if not path:
			path = '/'
		elif (not callable(getattr(path, '__getitem__', None)) or isinstance(path, unicode)):
			raise TypeError("path:%r is not a str or sequence." % path)
		if not host:
			host = self.getRequestHostname()
		elif not isinstance(host, str):
			raise TypeError("host:%r is not a str." % host)
		if not port:
			port = self.getHost().port
		elif not isinstance(port, int):
			raise TypeError("port:%r is not an int." % port)
		if query and (not callable(getattr(query, '__getitem__', None)) or isinstance(path, unicode)):
			raise TypeError("query:%r is not a dict or sequence." % query)
		
		if not isinstance(path, basestring):
			path = self.buildPath(path)
		
		return "{proto}://{host}{port}{path}{query}".format(
			proto='https' if secure else 'http',
			host=host,
			port=(":%i" % port) if port != (443 if secure else 80) else '',
			path=path,
			query=('?' + urllib.urlencode(query, True)) if query else ''
		)
		
	def addCookie(self, key, value, expires=None, domain=None, path=None, max_age=None, comment=None, secure=None):
		"""
		Sets an outgoing HTTP cookie.
		
		*key* (``str``) is the name of the cookie.
		
		*value* (``str``) is the value of the cookie.
		
		*expires* (``str`` or ``datetime``) optionally specifies the
		expiration of the cookie. Default is ``None``.
		
		*domain* (``str``) optionally specifies the cookie domain. Default
		is ``None``.
		
		*path* (``str``) optionally specifies the cookie path. Default is
		``None``.
		
		*max_age* (``int``) optionally is the number of seconds until the
		cookie expires. Default is ``None``.
		
		*comment* (``str``) optionally is the cookie comment. Default is
		``None``.
		
		*secure* (``bool``) is whether the cookie can only be communicated
		over a secure connection (``True``), or not (``False``). Default is
		``None`` for ``False``.
		"""
		# NOTE: Overrides ``twisted.web.http.Request.addCookie()``.
		if self._resp_started:
			raise ResponseStarted(self.path, "Response for %r has already started." % self)
		cookie = ["%s=%s" % (key, value)]
		if expires is not None:
			if isinstance(expires, (float, int, long, datetime.datetime)):
				expires = datetime_to_rfc2822(expires)
			cookie.append("Expires=%s" % expires)
		if domain is not None:
			cookie.append("Domain=%s" % domain)
		if path is not None:
			cookie.append("Path=%s" % path)
		if max_age is not None:
			cookie.append("Max-Age=%i" % max_age)
		if comment is not None:
			cookie.append("Comment=%s" % comment)
		if secure:
			cookie.append("Secure")
		self._resp_cookies.append("; ".join(cookie))
			
	def disableBuffering(self):
		"""
		Enables response output buffering.
		"""
		if self._resp_buffer:
			# Since we have buffered data, write it.
			self._write_buffer()
		self._resp_buffered = False
	
	def enableBuffering(self):
		"""
		Enables response output buffering.
		"""
		if self._resp_started:
			raise ResponseStarted(self.path, "Response for %r has already started." % self)
		self._resp_buffered = True
		
	def finish(self):
		"""
		Indicates that the request and its response are finished.
		"""
		# NOTE: Overrides ``twisted.web.http.Request.finish()``.
		if self.finished or self._disconnected:
			return
		if self._resp_buffered:
			# Since data is buffered data, write it.
			self._write_buffer(set_len=True)
		elif not self._resp_started:
			# Since header have not been written, write them.
			# Write headers because they have not been written.
			self._write_headers()
		if self._resp_chunked:
			# Write last chunk.
			self.transport.write("0\r\n\r\n")
		self.finished = True
		if not self.queued:
			# If this request is not currently queued, clean up.
			self._cleanup()
			
	def getCookie(self, key, default=None):
		"""
		Gets the specified request cookie.
		
		*key* (``str``) is the name of the cookie.
		
		*default* (**mixed**) optionally is the value to return if the
		cookie does not exist. Default is ``None``.
		
		Returns the cookie value (``str``) if it exists; otherwise,
		*default*.
		"""
		# NOTE: Overrides ``twisted.web.http.Request.getCookie()``.
		return self.received_cookies.get(key, default)
	
	def getHeader(self, key, default=None):
		"""
		Gets the specified request header.
		
		*key* (``str``) is the name of the header.
		
		*default* (**mixed**) optionally is the value to return if the
		header does not exist. Default is ``None``.
		
		Returns the header value (``str``) if it exists; otherwise,
		*default*. 
		"""
		# NOTE: Overrides ``twisted.web.http.Request.getHeader()``.
		return self.requestHeaders.getRawHeaders(key, default=(default,))[-1]
	
	def getRootURL(self):
		"""
		Gets the previously remembered URL.
		
		Returns the root URL (``str``).
		"""
		return self._root_url
	
	def prePathURL(self):
		"""
		Gets the absolute URL to the most nested resource which as yet been
		reached.
		
		Returns the pre-path URL (``str``).
		"""
		# NOTE: Derived from ``twisted.web.server.Request.prePathURL()``.
		return self.buildURL(self.prepath)
		
	def process(self):
		"""
		Called when the entire request has been received and is ready to be
		processed.
		"""
		# NOTE: Overrides ``twisted.web.http.Request.process()``.
		try:
			# Normalize path
			path = urllib.unquote(self.path)
			is_dir = path[-1:] == '/'
			path = os.path.normpath('/' + path.strip('/'))
			if is_dir and path != '/':
				path += '/'
				
			# Setup attributes.
			self.site = self.channel.site
			self.path = path
			self.prepath = []
			self.postpath = path.split('/')[1:]
			
			# Set default headers.
			self.setHeader('server', server.version)
			self.setHeader('date', datetime_to_rfc2822(time.time()))
		
			# Render request.
			resrc = self.site.getResourceFor(self)
			result = resrc.render(self)
			
			if isinstance(result, defer.Deferred):
				# Ensure request will be finished.
				result.addCallbacks(self._process_finish, self._process_error)
			
			elif isinstance(result, basestring):
				# Write result as body and finish request.
				self.write(result)
				self.finish()
				
			elif result is None:
				# Finish request.
				self.finish()
			
			elif result == server.NOT_DONE_YET:
				# Disable buffering because this causes NOT_DONE_YET resources
				# to hang (e.g., ``twisted.web.static.File``).
				self.disableBuffering()
			
			else:
				# Invalid result.
				raise ValueError("Resource:%r rendered result:%r is not a Deferred, string, None or NOT_DONE_YET:%r." % (resrc, result, server.NOT_DONE_YET))
				
		except Exception:
			self._process_error(failure.Failure())
			
	def _process_error(self, reason):
		"""
		Called when there is an error processing the request.
		
		*reason* (``twisted.internet.failure.Failure``) is the reason for
		failure.
		"""
		try:
			if not isinstance(reason, failure.Failure):
				# LIES! This is not an error.
				return
		
			# Log errors.
			log.err(reason, str(self))
		
			if self._disconnected or self.finished:
				# Since we are disconnected, return and do nothing.
				return
			
			if self._resp_error_cb:
				# Call error callback.
				try:
					self._resp_error_cb(self, reason, self._resp_started, *self._resp_error_args, **self._resp_error_kw)
				except Exception as e:
					log.err(e, str(self))
		
			if not self._resp_started:
				# Display Internal Server Error.
				code = http.INTERNAL_SERVER_ERROR
				if self.site.displayTracebacks:
					body = _server_traceback_html.format(path=urllib.escape(self.uri), traceback=webutil.formatFailure(reason))
				else:
					body = _server_error_html.format(path=urllib.escape(self.uri))
				self.setResponseCode(code)
				self.setResponseEncoding(self._resp_enc or 'UTF-8')
				self.write(body)
			
			elif 'text/html' in self.responseHeaders.getRawHeaders('content-type', ('',))[0]:
				# Since an error occured but we've already started writing the
				# response, do what we can.
				if self.site.displayTracebacks:
					body = _server_traceback_html.format(path=urllib.escape(self.uri), traceback=webutil.formatFailure(reason))
				else:
					body = "<h1>...Internal Server Error!</h1>"
				self.write(body)
				
		except Exception as e:
			log.err(e, str(self))
			
		finally:
			self.finish()
			
	def _process_finish(self, _):
		"""
		Called at the end of the deferred chain to ensure that the request
		is finished.
		
		*_* is ignored.
		"""
		self.finish()
		
	def redirect(self, url):
		"""
		Utility function that does a redirect. The request should have
		*finish()* called after this.
		"""
		# NOTE: Overrides ``twisted.web.http.Request.redirect()``.
		if self._resp_started:
			raise ResponseStarted(self.path, "Response for %r has already started." % self)
		if not isinstance(url, str):
			raise TypeError("url:%r is not a str." % url)
		
		self.setResponseCode(http.FOUND)
		self.setHeader('location', url)
		
	def rememberRootURL(self, url=None):
		"""
		Remembers the currently processed part of the URL for later
		recalling.
		
		*url* (``str``) is the URL to remember. Default is ``None`` for
		twisted's implementation of using *prepath* minus the last segment.
		"""
		# NOTE: Derived from ``twisted.web.server.Request.rememberRootURL()``.
		self._root_url = url if url else self.buildURL(self.prepath[:-1])
		
	def setContentType(self, content_type):
		"""
		Sets the content type for the response.
		
		*content_type* (``str``) is the content type of the response.
		"""
		if self._resp_started:
			raise ResponseStarted(self.path, "Response for %r has already started." % self)
		if not isinstance(content_type, str):
			raise TypeError("content_type:%r is not a str." % content_type)
		elif not content_type:
			raise ValueError("content_type:%r cannot be empty." % content_type)
		self._resp_content_type = content_type
		
	def setHeader(self, key, value):
		"""
		Sets the HTTP response header, overriding any previously set values
		for this header.
		
		*key* (``str``) is the name of the header.
		
		*value* (``str``) is the value of the header.
		"""
		# NOTE: Overrides ``twisted.web.http.Request.setHeader()``.
		if self._resp_started:
			raise ResponseStarted(self.path, "Response for %r has already started." % self)
		if not isinstance(key, str):
			raise TypeError("key:%r is not a str." % key)
		if not isinstance(value, str):
			value = str(value)
		self.responseHeaders.setRawHeaders(key, [value])
		
	def setLastModified(self, when):
		"""
		Sets the last modified time for the response.
		
		If this is called more than once, the latest Last-Modified value
		will be used.
		
		If this is a conditional request (i.e., If-Modified-Since header was
		received), the response code will be set to Not Modified.
		
		*when* (``float`` or ``datetime``) is that last time the resource
		was modified. If a ``float``, then the seconds since the UNIX epoch.
		
		Returns ``twisted.web.http.CACHED`` if this is condition request and
		If-Modified-Since is ealier than Last-Modified; otherwise, ``None``.
		"""
		# NOTE: Overrides ``twisted.web.http.Request.setLastModified()``.
		if self._resp_started:
			raise ResponseStarted(self.path, "Response for %r has already started." % self)
			
		if not isinstance(when, datetime.datetime):
			if isinstance(when, (float, int, long)):
				when = datetime.datetime.fromtimestamp(when, _utc)
			else:
				raise TypeError("when:%r is not a float or datetime." % when)
		if not self._resp_last_modified or when > self._resp_last_modified:
			self._resp_last_modified = when
		
		mod_since = self.requestHeaders.getRawHeaders('if-modified-since', (None,))[0]
		if mod_since:
			try:
				mod_since = dt_parser.parse(mod_since.split(';', 1)[0])
			except ValueError:
				return
			if mod_since >= when:
				self.setResponseCode(http.NOT_MODIFIED)
				return http.CACHED
		
	def setResponseCode(self, code, message=None):
		"""
		Sets the HTTP response status code.
		
		*code* (``int``) is the status code.
		
		*message* (``str``) is a custom status code message. Default is
		``None`` for the default status code message. 
		"""
		if self._resp_started:
			raise ResponseStarted(self.path, "Response for %r has already started." % self)
		if not isinstance(code, int):
			raise TypeError("code:%r is not an int." % code)
		if message is not None and not isinstance(message, str):
			raise TypeError("message:%r is not a str." % message)
		self._resp_code = code
		self._resp_code_message = message or http.RESPONSES.get(code, "Unknown Status")
		
	def setResponseEncoding(self, encoding):
		"""
		Sets the response encoding.
		
		*encoding* (``str``) is the response encoding.
		"""
		try:
			codecs.lookup(encoding)
		except LookupError:
			raise ValueError("encoding:%r is not recognized." % encoding)
		self._resp_enc = encoding
		
	def getSession(self):
		# TODO
		raise NotImplementedError("TODO: getSession()")
		
	def setErrorCallback(self, callback, args=None, kwargs=None):
		"""
		Sets the callback function which will be called when there is an
		error rendering a resource.
		
		*callback* (**callable**) is the method called on an error. This
		will be passed: *request*, *reason* and *started*
		
		- *request* (``lib.twisted.DeferredRequest``) is the request.
		
		- *reason* (``twisted.python.failure.Failure``) describes the error that
		  occured.
		
		- *started* (``bool``) is whether the response has started
		  (``True``), or not (``False``).
		
		If a custom error response is to be written the to *request*,
		*started* should be checked because this indicates whether a
		response was already started.
		
		*args* (**sequence**) contains any positional arguments to pass to
		*callback*. Default is ``None`` for no positional arguments.
		
		*kwargs* (``dict``) contains any keyword arguments to pass to
		*callback*. Default is ``None`` for not keyword arguments.
		"""
		self._resp_error_cb = callback
		self._resp_error_args = args or ()
		self._resp_error_kw = kwargs or {}
		
	def URLPath(self):
		"""
		Gets the URL Path that identifies this requested URL.
		
		Returns the URL Path (``twisted.python.urlpath.URLPath``).
		"""
		# NOTE: Derived from ``twisted.web.server.Request.URLPath()``.
		return urlpath.URLPath.fromString(self.prePathURL())
				
	def write(self, data):
		"""
		Writes (or buffers) some data in response to this request while
		ensuring that the data is encoded properly.
		
		.. NOTE: To bypass encoding, use *write_raw()*.
		
		*data* (**string**) is the data to write.
		"""
		# NOTE: Overrides ``twisted.web.http.Request.write()``.
		if self._resp_enc:
			self.write_raw(unicode(data).encode(self._resp_enc))
		elif isinstance(data, str):
			self.write_raw(data) 
		elif isinstance(data, unicode):
			raise UnicodeError("data:%r is unicode, but no response encoding set." % data)
		else:
			raise TypeError("data:%r is not a string." % data)
		
	def write_raw(self, data):
		"""
		Writes (or buffers) some data in response to this request.
		
		Arguments:
		- data (``str``)
		  - The data to write.
		"""
		if data and not self._resp_nobody:
			if self._resp_buffered:
				if not self._resp_buffer:
					self._resp_buffer = StringIO.StringIO()
				self._resp_buffer.write(data)
			else:
				self._write_response(data)
	
	def _write_buffer(self, set_len=None):
		"""
		Writes (flushes) the response buffer.
		
		*set_len* (``bool``) is whether the Content-Length header should be
		set to the amount of buffered data. Default is ``None`` for
		``False``.
		"""
		if self._resp_buffer:
			data = self._resp_buffer.getvalue()
			self._resp_buffer = None
		else:
			data = ''
		if set_len:
			self.setHeader('content-length', len(data))
		self._write_response(data)
		
	def _write_headers(self):
		"""
		Writes the response headers.
		"""
		# NOTE: Derived from ``twisted.web.http.Request.write()``. 
		if self._disconnected:
			raise RequestDisconnected("Request %r is disconnected." % self)
		if self._resp_started:
			raise RuntimeError("Request %r already started." % self)
			
		version = self.clientproto.upper()
		method = self.method.upper()
		
		# Write first line.
		lines = ["%s %s %s\r\n" % (version, self._resp_code, self._resp_code_message)]
		
		# Determine if the there should be a response body and if it is
		# going to be chunked.
		if method == 'HEAD' or self._resp_code in http.NO_BODY_CODES:
			self._resp_nobody = True
			self.responseHeaders.removeHeader('content-type')
			self.responseHeaders.removeHeader('content-length')
		else:
			if not self.responseHeaders.hasHeader('content-type'):
				# Ensure a content type is set.
				if self._resp_content_type == 'text/html' and self._resp_enc:
					self.setHeader('content-type', 'text/html; charset=' + self._resp_enc)
				else:
					self.setHeader('content-type', self._resp_content_type)
			
			if not self.responseHeaders.hasHeader('last-modified') and self._resp_last_modified:
				self.setHeader('last-modified', datetime_to_rfc2822(self._resp_last_modified))
				
			if version == 'HTTP/1.1' and not self.responseHeaders.hasHeader('content-length'):
				# If no content length was set, use chunking.
				lines.append("Transfer-Encoding: chunked\r\n")
				self._resp_chunked = True
		
		# Write headers.
		for name, values in self.responseHeaders.getAllRawHeaders():
			for value in values:
				lines.append("%s: %s\r\n" % (name, value))
		
		# Write cookies.
		for cookie in self._resp_cookies:
			lines.append("Set-Cookie: %s\r\n" % cookie)
		
		# Write end of headers.
		lines.append("\r\n")
		
		# Actually write headers.
		self._resp_started = True
		self.transport.writeSequence(lines)
		
	def _write_response(self, data):
		"""
		Actually writes data in response to this request.
		
		*data* (``str``) is the data to write.
		"""
		# NOTE: Derived from ``twisted.web.http.Request.write()``. 
		if self._disconnected:
			raise RequestDisconnected("Request %r is disconnected." % self)
		if not self._resp_started:
			self._write_headers()
			if self._resp_nobody:
				return
		if data:
			self.sentLength += len(data)
			if self._resp_chunked:
				self.transport.writeSequence(http.toChunk(data))
			else:
				self.transport.write(data)
		
