
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